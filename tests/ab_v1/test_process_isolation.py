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
    monkeypatch.setenv("OPERATOR_TOKEN", "synthetic-operator-secret")
    command = [sys.executable, "-c", "import os,sys; sys.stdin.buffer.read(); print('MODEL_API_KEY' in os.environ or 'OPERATOR_TOKEN' in os.environ)"]
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
