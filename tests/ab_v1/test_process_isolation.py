import importlib.util
import os
import sys

import pytest


def runner():
    assert importlib.util.find_spec("app.runtime.processes") is not None
    from app.runtime.processes import run_bounded
    return run_bounded


def test_child_environment_has_no_parent_credentials(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "synthetic-model-secret")
    command = [sys.executable, "-c", "import os,sys; sys.stdin.buffer.read(); print('MODEL_API_KEY' in os.environ)"]
    result = runner()(command, b"{}", timeout=3)
    assert result["stdout"].strip() == b"False"
    assert os.environ["MODEL_API_KEY"] == "synthetic-model-secret"


def test_bounded_process_returns_real_result_without_retries():
    command = [sys.executable, "-c", "import sys; x=sys.stdin.buffer.read(); sys.stdout.buffer.write(x)"]
    result = runner()(command, b'{"ok":true}', timeout=3)
    assert result["stdout"] == b'{"ok":true}'
    assert result["returncode"] == 0


def test_timeout_terminates_actual_child_process():
    command = [sys.executable, "-c", "import sys,time; sys.stdin.buffer.read(); time.sleep(120)"]
    with pytest.raises(TimeoutError):
        runner()(command, b"{}", timeout=0.2)


def test_total_output_is_bounded_across_both_streams():
    command = [sys.executable, "-c", "import sys; sys.stdin.buffer.read(); sys.stdout.write('a'*700); sys.stderr.write('b'*700)"]
    with pytest.raises(ValueError, match="output limit"):
        runner()(command, b"{}", timeout=3, max_output=1024)


def test_already_cancelled_request_does_not_start_a_process(tmp_path):
    import threading
    cancelled = threading.Event()
    cancelled.set()
    marker = tmp_path / "started.txt"
    code = "import pathlib,sys; sys.stdin.buffer.read(); pathlib.Path(sys.argv[1]).write_text('started')"
    with pytest.raises(InterruptedError):
        runner()([sys.executable, "-c", code, str(marker)], b"{}", timeout=3, cancelled=cancelled)
    assert not marker.exists()


def test_cancellation_after_spawn_is_checked_before_releasing_request(tmp_path):
    class Cancellation:
        calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls >= 2

    marker = tmp_path / "executed.txt"
    code = "import pathlib,sys; x=sys.stdin.buffer.read(); pathlib.Path(sys.argv[1]).write_bytes(x) if x else None"
    with pytest.raises(InterruptedError):
        runner()([sys.executable, "-c", code, str(marker)], b"private-request", timeout=3, cancelled=Cancellation())
    assert not marker.exists()


def test_cancelled_result_cannot_be_accepted_after_child_exit(tmp_path):
    marker = tmp_path / "cancelled.txt"

    class Cancellation:
        def is_set(self):
            return marker.exists()

    code = "import pathlib,sys; sys.stdin.buffer.read(); pathlib.Path(sys.argv[1]).write_text('cancelled'); print('result')"
    with pytest.raises(InterruptedError):
        runner()([sys.executable, "-c", code, str(marker)], b"{}", timeout=3, cancelled=Cancellation())


def test_exchange_has_an_operation_deadline_even_with_live_heartbeats():
    import threading
    cancelled = threading.Event()
    received = []
    code = ("import json,sys,time,threading; sys.stdin.buffer.readline(); "
            "print(json.dumps({'type':'progress','sequence':1}),flush=True); "
            "exec('def beat():\\n while True:\\n  time.sleep(0.02)\\n  print(json.dumps({\"type\":\"heartbeat\"}),flush=True)'); "
            "threading.Thread(target=beat,daemon=True).start(); sys.stdin.buffer.readline()")
    def exchange(value):
        if value['type'] == 'heartbeat':
            received.append(value)
            return None
        cancelled.wait(2)
        return {'continue':True}
    with pytest.raises(TimeoutError) as caught:
        runner()([sys.executable, '-c', code], b'{}\n', timeout=None,
                 operation_timeout=0.15, cancelled=cancelled, exchange=exchange)
    assert received
    assert caught.value.code == 'operation_timeout'
    assert caught.value.stage == 'exchange' and caught.value.check_id == 'step:1'
    assert cancelled.is_set()


def test_process_failure_has_safe_stage_category_and_check_identifier():
    code = "import sys,time; sys.stdin.buffer.read(); time.sleep(120)"
    with pytest.raises(TimeoutError) as caught:
        runner()([sys.executable, '-c', code], b'password=synthetic-secret', timeout=0.1)
    error = caught.value
    assert error.code == 'process_timeout' and error.stage == 'execute'
    assert error.check_id == 'process' and error.diagnostic['error_category'] == 'deadline'
    assert 'synthetic-secret' not in str(error.diagnostic)


def test_malformed_worker_message_is_not_misclassified_as_output_limit():
    command = [sys.executable, '-c', "import sys,time; sys.stdin.buffer.readline(); print('password=synthetic-secret',flush=True); time.sleep(5)"]
    with pytest.raises(ValueError) as caught:
        runner()(command, b'{}\n', timeout=2, exchange=lambda value: None)
    assert caught.value.code == 'worker_message_invalid'
    assert caught.value.stage == 'receive'
    assert 'synthetic-secret' not in str(caught.value.diagnostic)
