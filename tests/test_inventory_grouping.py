from app.connectors.mock import MockConnector
from test_business import onboard
from test_runtime import discover, target


def test_process_grouping_preserves_evidence_and_prioritizes_services(client, monkeypatch):
    profile = onboard(client)
    t = target(client, profile, docker=True)
    original = MockConnector.read
    def read(self, tool, args):
        if tool == "process.list":
            return {"processes": [{"id": str(i), "pid": i, "name": "python", "state": "S", "kind": "process"} for i in range(1, 61)]}
        return original(self, tool, args)
    monkeypatch.setattr(MockConnector, "read", read)
    r = discover(client, profile, t)
    group = r["process_groups"][0]
    assert group["count"] == 60 and len(group["processes"]) == 60
    assert all(p["evidence_id"] and p["classification"] == "insufficient_evidence" for p in group["processes"])
    assert r["runtime_objects"][0]["kind"] == "service"
    assert len(next(o for o in r["observations"] if o["tool_id"] == "process.list")["payload"]["processes"]) == 60
    unrelated = next(x for x in r["runtime_objects"] if x["name"] == "unrelated-backup")
    assert unrelated["classification"] == "unmatched"


def test_rejected_object_is_unrelated_not_unmatched_or_auto_reproposed(client):
    profile = onboard(client)
    t = target(client, profile)
    r = discover(client, profile, t)
    binding = next(x for x in r["matches"] if x["kind"] == "service")
    client.post(f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/review",
                json={"decision": "reject", "reason": "此隔离对象不属于当前业务范围"}).raise_for_status()
    r = discover(client, profile, t)
    item = next(x for x in r["runtime_objects"] if x["id"] == binding["runtime_candidate"])
    assert item["classification"] == "unrelated"
    assert not any(x["runtime_candidate"] == item["id"] and x["kind"] == "service" for x in r["matches"])
