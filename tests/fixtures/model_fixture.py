"""Local Docker test provider. Does not contact an upstream or log prompts/keys."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALLS = 0


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global CALLS
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 200000:
            self.send_error(413)
            return
        request = json.loads(self.rfile.read(size))
        context = json.loads(request["messages"][1]["content"])
        CALLS += 1
        report = {"summary": "隔离 Fake Provider 无足够根因证据", "hypotheses": [], "next_action": None, "action": None}
        if context.get("phase") == "discovery":
            report = {"summary": "隔离 Fake Provider 提出待审候选映射", "proposals": []}
            candidate = next((c for c in context["candidates"] if c["kind"] == "service" and c["id"] == "catalog-api.service"), None)
            service = next((s for s in context["services"] if s["name"] == "catalog-api"), None)
            if candidate and service:
                report["proposals"] = [{"service": service["name"], "role": service["role"], "candidate_key": candidate["candidate_key"],
                    "kind": "service", "evidence_ids": candidate["evidence_ids"], "reasons": ["合成清单名称相同，尚需人工审核"],
                    "counterevidence": ["名称并不证明业务身份"], "uncertainty": "未经范围确认", "next_tools": ["service.status", "service.logs"]}]
        body = {"id": "isolated-provider", "object": "chat.completion", "created": 1, "model": request["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(report)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120}}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
