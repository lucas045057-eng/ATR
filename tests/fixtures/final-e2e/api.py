"""Actual TCP dependency check and HTTP endpoint; no fault answer is passed to ATR."""

import json
import os
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer

host, port = os.environ["DB_HOST"], int(os.environ["DB_PORT"])


def check_database():
    with socket.create_connection((host, port), timeout=2):
        pass


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            check_database()
            status, body = 200, {"status": "ok", "database_tcp_connected": True}
        except OSError:
            status, body = 503, {"status": "dependency_unreachable"}
        self.send_response(status)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *_):
        pass


try:
    check_database()
except OSError as exc:
    print(
        f"startup failed: application exited because database connection failed at {host}:{port}; {type(exc).__name__}",
        flush=True,
    )
    raise SystemExit(1) from None
print("ready: database TCP connected; HTTP listener bound on 8000", flush=True)
HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
