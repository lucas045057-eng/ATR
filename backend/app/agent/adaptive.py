"""Bounded investigation choices; authorization and evidence remain separate gates."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from app.common import digest
from app.connectors.policy import check
from app.system.scope import binding_is_current
from app.tools.redaction import redact_text


def action_id(action):
    return digest({k: action.get(k) for k in ("tool_id", "target_node_id", "target_id", "arguments")})


def public_action(action):
    return {k: action[k] for k in ("action_id", "tool_id", "target_node_id", "service", "role", "arguments", "scheduling") if k in action}


def current_graph(graph, bindings):
    current_ids = {b["id"] for b in bindings if binding_is_current(b)}
    nodes = [n for n in graph["nodes"] if n.get("role") != "runtime" or n.get("attributes", {}).get("binding_id") in current_ids]
    ids = {n["id"] for n in nodes}
    return {**graph, "nodes": nodes, "edges": [e for e in graph["edges"] if e["from_node_id"] in ids and e["to_node_id"] in ids]}


class AdaptiveInvestigationPlanner:
    def __init__(self, graph, bindings, targets, symptom, root_id, settings):
        self.graph, self.bindings, self.targets = graph, bindings, targets
        self.symptom, self.root_id, self.settings = symptom, root_id, settings
        self.nodes = {n["id"]: n for n in graph["nodes"] if n.get("kind") == "service" and n.get("role") != "runtime"}
        self.hypotheses, self.hypothesis_history, self.scope_requests = [], [], []

    def candidates(self, evidence, code_context, history):
        active = {h["target_node_id"] for h in self.hypotheses if h["status"] != "refuted"}
        focus = {self.root_id} | active | {e.get("target_node_id") for e in evidence}
        related = set(focus)
        for edge in self.graph["edges"]:
            if edge.get("relation") in {"depends_on", "calls", "routes_to"}:
                if edge["from_node_id"] in focus:
                    related.add(edge["to_node_id"])
                if edge["to_node_id"] in focus:
                    related.add(edge["from_node_id"])
        fresh = [b for b in self.bindings if binding_is_current(b) and b["kind"] in {"service", "container"}]
        authorized_names = {b["service"] for b in fresh}
        self.scope_requests = [{"service": self.nodes[node_id]["name"], "reason": "关联服务尚无当前已确认的运行绑定"}
                               for node_id in sorted(related & self.nodes.keys())
                               if self.nodes[node_id]["name"] not in authorized_names][:8]
        used = {h.get("action_id") for h in history}
        choices = {}

        def add(tool, node_id, arguments, target_id=None, binding_id=None):
            node = self.nodes.get(node_id)
            if not node:
                return
            action = {"tool_id": tool, "target_node_id": node_id, "target_id": target_id,
                      "service": node["name"], "role": node["role"], "arguments": arguments}
            if binding_id:
                action["binding_id"] = binding_id
            if target_id:
                config = self.targets.get(target_id)
                approved = {(b["kind"], b["runtime_id"]) for b in fresh if b["target_id"] == target_id}
                try:
                    check(config, tool, arguments, approved)
                except (ValueError, PermissionError, TypeError, KeyError):
                    return
            identifier = action_id(action)
            if identifier in used:
                return
            action["action_id"] = identifier
            relevance = 2 if node["name"].casefold() in self.symptom.casefold() else 1
            discrimination = 2 if node_id in active else 1
            if any(p.get("tool_id") == tool and p.get("target_node_id") == node_id
                   for h in self.hypotheses for p in h.get("verification_predictions", [])):
                discrimination += 1
            cost = {"status": 0.1, "logs": 0.4, "config.list": 0.2, "config.read": 0.6,
                    "code.search": 0.2, "code.context": 0.5}.get(tool, 0.2)
            if tool.endswith("status"):
                cost = 0.1
            elif tool.endswith("logs"):
                cost = 0.4
            action["scheduling"] = {"symptom_relevance": relevance, "hypothesis_discrimination": discrimination,
                                    "expected_information": 1, "read_cost": cost, "context_cost": cost,
                                    "already_executed": False, "permission_allowed": True,
                                    "score": relevance + discrimination + 2 - 2 * cost}
            choices[identifier] = action

        for node_id in sorted(related & self.nodes.keys()):
            name = self.nodes[node_id]["name"]
            for binding in fresh:
                if binding["service"] != name:
                    continue
                args = {"name": binding["runtime_id"]} if binding["kind"] == "service" else {"container_id": binding["runtime_id"]}
                for suffix in ("status", "logs"):
                    add(f"{binding['kind']}.{suffix}", node_id, args, binding["target_id"], binding["id"])
                for path in self.targets.get(binding["target_id"], {}).get("allowed_paths", [])[:2]:
                    add("config.list", node_id, {"path": path}, binding["target_id"], binding["id"])
        for entry in evidence:
            if entry.get("tool_id") != "config.list":
                continue
            target_id = entry.get("target_id")
            binding = next((b for b in fresh if b["target_id"] == target_id and
                            self.nodes.get(entry["target_node_id"], {}).get("name") == b["service"]), None)
            if not binding:
                continue
            files = entry.get("payload", {}).get("files", [])
            if not isinstance(files, list):
                continue
            for path in files[:8]:
                if isinstance(path, str) and (PurePosixPath(path).name == ".env" or PurePosixPath(path).suffix.lower() in {".conf", ".ini", ".cfg", ".env", ".yaml", ".yml", ".json", ".toml", ".properties"}):
                    add("config.read", entry["target_node_id"], {"path": path}, target_id, binding["id"])
        if self.root_id:
            keywords = list(dict.fromkeys(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", self.symptom)))[:4]
            for word in keywords:
                add("code.search", self.root_id, {"keyword": word[:120], "limit": min(5, self.settings.code_search_limit)})
            for context in code_context:
                for result in context.get("results", [])[:5]:
                    if isinstance(result, dict) and isinstance(result.get("file_id"), str):
                        add("code.context", self.root_id, {"file_id": result["file_id"], "max_lines": 50})
        return sorted(choices.values(), key=lambda a: (-a["scheduling"]["score"], a["action_id"]))[:12]

    def update(self, report, gate, round_number):
        previous = {h["hypothesis_id"]: h for h in self.hypotheses}
        updates = []
        raw = report.get("hypotheses", [])
        for item in gate.get("hypotheses", [])[:3]:
            original = next((h for h in raw if isinstance(h, dict) and h.get("target_node_id") == item["target_node_id"] and str(h.get("failure_mode", "candidate"))[:500] == item["failure_mode"]), {})
            key = digest({"target_node_id": item["target_node_id"], "failure_mode": item["failure_mode"]})
            links = item.get("evidence_links", [])
            supports = [e["evidence_id"] for e in links if e["relation"] == "supports"]
            old = next((h for h in reversed(self.hypothesis_history) if h["hypothesis_id"] == key), previous.get(key, {}))
            refutes = sorted(set(old.get("counter_evidence", [])) | {e["evidence_id"] for e in links if e["relation"] == "refutes"})
            links = list({(e["evidence_id"], e["relation"]): e for e in old.get("evidence_links", []) + links}.values())
            status = "refuted" if refutes else "supported" if item["status"] == "supported" else "candidate"
            missing = original.get("missing_evidence", [])
            if not isinstance(missing, list):
                missing = []
            missing = list(dict.fromkeys(str(x) for x in missing + item.get("verification", {}).get("missing_evidence", [])))
            predictions = original.get("verification_predictions", [])
            if not isinstance(predictions, list):
                predictions = []
            predictions = [{k: p.get(k) for k in ("target_node_id", "tool_id", "field", "operator", "value")}
                           for p in predictions[:4] if isinstance(p, dict)
                           and p.get("field") in {"state", "status_code", "listening", "log_signal"}
                           and p.get("operator") in {"eq", "ne", "contains"}
                           and isinstance(p.get("value"), (str, int, float, bool))]
            entry = {**item, "hypothesis_id": key, "status": status, "round": round_number,
                     "evidence_links": links,
                     "support_evidence": supports, "counter_evidence": refutes,
                     "missing_evidence": [redact_text(str(x))[:160] for x in missing[:4]],
                     "verification_predictions": predictions}
            if status == "candidate" and not entry["missing_evidence"]:
                entry["missing_evidence"] = ["需要故障窗口内独立运行证据与可核验预测"]
            self.hypothesis_history.append(entry)
            updates.append(entry)
            previous.pop(key, None)
        for item in previous.values():
            if item["status"] != "refuted" and len([h for h in updates if h["status"] != "refuted"]) < 3:
                entry = {**item, "round": round_number, "status": "unresolved"}
                updates.append(entry)
                self.hypothesis_history.append(entry)
        self.hypotheses = updates
        return updates

    def requested_scope(self, report):
        requested = report.get("scope_requests", [])
        if not isinstance(requested, list):
            return []
        authorized = {b["service"] for b in self.bindings if binding_is_current(b)}
        blocked = [{"service": redact_text(str(r.get("service", "")))[:80], "reason": redact_text(str(r.get("reason", "需要额外服务授权")))[:300]}
                   for r in requested[:4] if isinstance(r, dict) and isinstance(r.get("service"), str)
                   and r["service"].strip() and r["service"] not in authorized]
        self.scope_requests = list({r["service"]: r for r in self.scope_requests + blocked}.values())[:8]
        return blocked
