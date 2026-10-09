"""Isolated real Python process faults; store/cache/edge are TCP test doubles."""

import argparse
import socket
import time
from datetime import datetime, timedelta, timezone


def log(text, historical=False):
    stamp = datetime.now(timezone.utc) - (timedelta(days=3) if historical else timedelta())
    print(f"{stamp.isoformat()} {text}", flush=True)


def refused(port):
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        raise AssertionError("Isolated endpoint unexpectedly available")


def run(case, service):
    if case == "application_exit" and service == "catalog-api":
        raise RuntimeError("startup failed: application exited because required initialization failed")
    if case in {"database_unavailable", "multiple_services", "permission_blocked"} and service == "store":
        try:
            refused(15432)
        except OSError as error:
            raise RuntimeError("FATAL database unavailable: database startup failed") from error
    if case in {"redis_unavailable", "multiple_services"} and service == "cache":
        try:
            refused(16379)
        except OSError as error:
            raise RuntimeError("Redis unavailable: redis startup failed") from error
    if case == "upstream_unavailable" and service == "catalog-api":
        try:
            refused(18080)
        except OSError as error:
            raise RuntimeError("upstream connection refused; requests cannot complete") from error
    if case == "dependency_wrong" and service == "catalog-api":
        try:
            refused(25432)
        except OSError as error:
            raise RuntimeError("dependency route wrong: configured store endpoint is 25432") from error
    if case == "file_permission" and service == "catalog-api":
        try:
            with open("/root/atr-fixture-private.conf"):
                raise AssertionError("Unexpected access to root-only directory")
        except PermissionError as error:
            raise RuntimeError("startup failed: permission denied reading restricted configuration") from error
    if case == "bind_failure" and service == "catalog-api":
        with socket.socket() as occupied, socket.socket() as duplicate:
            occupied.bind(("127.0.0.1", 18123))
            occupied.listen()
            try:
                duplicate.bind(("127.0.0.1", 18123))
            except OSError as error:
                raise RuntimeError("startup failed: failed to bind, address already in use") from error
    if service == "catalog-api" and case in {"database_unavailable", "permission_blocked", "multiple_services"}:
        log("store connection refused; requests unavailable")
    if service == "catalog-api" and case == "redis_unavailable":
        log("cache connection refused; requests unavailable")
    if service == "catalog-api" and case == "unrelated_error":
        log("ERROR telemetry exporter retry; request path remains healthy")
    if service == "catalog-api" and case == "historical_error":
        log("startup failed: old deployment incident", historical=True)
    listener = None
    if not (service == "catalog-api" and case == "port_unbound"):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 18124 if case == "port_mismatch" else 18123))
        listener.listen()
        log(f"active listener port={listener.getsockname()[1]}")
    else:
        log("active process without the configured business listener")
    log("ready")
    time.sleep(300)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--service", choices=["catalog-api", "store", "cache"], required=True)
    options = parser.parse_args()
    try:
        run(options.case, options.service)
    except (OSError, RuntimeError) as error:
        log(str(error))
        raise SystemExit(1) from error
