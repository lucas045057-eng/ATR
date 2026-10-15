"""Deadline-enforced process trees. Children must await their private stdin request."""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ENVIRONMENT_KEYS = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}


def process_failure(kind, message, code, stage, check_id='process', category='protocol'):
    error = kind(message)
    error.code, error.stage, error.check_id = code, stage, check_id
    error.diagnostic = {'code': code, 'failure_stage': stage, 'stage': stage,
                        'error_category': category, 'check_id': check_id, 'exception_type': kind.__name__}
    return error


def child_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() in ENVIRONMENT_KEYS}
    env.update(PYTHONUTF8="1", PYTHONPATH=str(Path(__file__).resolve().parents[2]),
               LITELLM_LOCAL_MODEL_COST_MAP="True", LITELLM_MODE="PRODUCTION")
    return env


def _windows_job(process):
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                    ("max_working", ctypes.c_size_t), ("active", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; descendants inherit this job.
    if not handle:
        raise OSError("Process supervision unavailable")
    if (not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not kernel.AssignProcessToJobObject(handle, int(process._handle))):
        kernel.CloseHandle(handle)
        raise OSError("Process supervision unavailable")
    return kernel, handle


def run_bounded(arguments: list[str], request: bytes, *, timeout: float | None, max_output: int = 2 * 1024 * 1024,
                cancelled: threading.Event | None = None, on_spawn=None, exchange=None, on_message=None,
                on_progress=None, operation_timeout: float = 90) -> dict:
    if (timeout is not None and timeout <= 0) or operation_timeout <= 0 or len(request) > 2 * 1024 * 1024:
        raise process_failure(ValueError, "Invalid process request bounds", 'process_request_invalid', 'validate')
    if cancelled is not None and cancelled.is_set():
        raise process_failure(InterruptedError, "Process request cancelled", 'process_cancelled', 'dispatch', category='cancellation')
    deadline = time.monotonic() + timeout if timeout is not None else float('inf')
    if sys.platform == "linux":
        arguments = [sys.executable, "-m", "app.runtime.process_bootstrap", *arguments]
    owner_cookie = uuid.uuid4().hex
    environment = {**child_environment(), 'ATR_PROCESS_OWNER': owner_cookie}
    try:
        process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=environment,
                                   start_new_session=sys.platform != "win32")
    except OSError:
        raise process_failure(OSError, 'Process spawn failed', 'process_spawn_failed', 'spawn', category='execution') from None
    job = None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    exceeded = threading.Event()
    read_failure = {'code': 'process_output_limit', 'category': 'output_limit'}
    readers = []
    buffer_lock = threading.Lock()
    incoming, outgoing = queue.Queue(maxsize=64), queue.Queue(maxsize=2)
    dispatch = None
    write_lock = threading.Lock()
    operation_deadline = time.monotonic() + operation_timeout
    operation_check = 'step:0'

    def send(value):
        encoded = json.dumps(value, ensure_ascii=False).encode() + b'\n'
        if len(encoded) > max_output:
            raise process_failure(ValueError, 'Process reply limit exceeded', 'process_reply_limit', 'exchange', operation_check, 'output_limit')
        with write_lock:
            process.stdin.write(encoded)
            process.stdin.flush()

    def read(stream, name):
        pending = bytearray()
        try:
            while chunk := stream.read1(4096):
                # Broker messages are consumed as a stream; bound each message, not lifetime heartbeats.
                if exchange is None or name != 'stdout':
                    with buffer_lock:
                        if sum(len(value) for value in buffers.values()) + len(chunk) > max_output:
                            exceeded.set()
                            break
                        buffers[name].extend(chunk)
                if (exchange is not None or on_progress is not None) and name == 'stdout':
                    pending.extend(chunk)
                    while b'\n' in pending:
                        line, _, pending = pending.partition(b'\n')
                        if exchange is not None and len(line) > min(65536, max_output):
                            exceeded.set()
                            break
                        item = json.loads(line)
                        if exchange is not None:
                            incoming.put_nowait(item)
                        elif on_progress is not None:
                            on_progress(item)
                    if exceeded.is_set() or len(pending) > (min(65536, max_output) if exchange is not None else max_output):
                        exceeded.set()
                        break
            if exchange is not None and pending:
                exceeded.set()
        except (ValueError, UnicodeError):
            read_failure.update(code='worker_message_invalid', category='protocol')
            exceeded.set()
        except queue.Full:
            read_failure.update(code='worker_queue_limit', category='protocol')
            exceeded.set()
        except OSError:
            read_failure.update(code='process_channel_failed', category='transport')
            exceeded.set()

    def write():
        try:
            if (cancelled is not None and cancelled.is_set()) or time.monotonic() >= deadline:
                process.stdin.close()
                return
            process.stdin.write(request)
            if exchange is None:
                process.stdin.close()
            else:
                process.stdin.flush()
        except (OSError, ValueError):
            pass

    try:
        if sys.platform == "win32":
            try:
                job = _windows_job(process)
            except OSError:
                raise process_failure(OSError, 'Process supervision unavailable', 'process_containment_unavailable', 'containment', category='containment') from None
        if on_spawn is not None:
            on_spawn({'platform': sys.platform, 'pid': process.pid, 'cookie': owner_cookie})
        if (cancelled is not None and cancelled.is_set()) or time.monotonic() >= deadline:
            raise process_failure(InterruptedError, "Process dispatch no longer authorized", 'process_cancelled', 'dispatch', category='cancellation')
        for name in buffers:
            thread = threading.Thread(target=read, args=(getattr(process, name), name), daemon=True)
            thread.start()
            readers.append(thread)
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        while process.poll() is None:
            if cancelled is not None and cancelled.is_set():
                raise process_failure(InterruptedError, "Process request cancelled", 'process_cancelled', 'execute', category='cancellation')
            if exceeded.is_set():
                raise process_failure(ValueError, "Process output limit exceeded" if read_failure['category'] == 'output_limit'
                                      else 'Invalid worker output channel', read_failure['code'], 'receive', category=read_failure['category'])
            if time.monotonic() >= deadline:
                raise process_failure(TimeoutError, "Process request deadline exceeded", 'process_timeout', 'execute', category='deadline')
            if exchange is not None:
                if time.monotonic() >= operation_deadline:
                    raise process_failure(TimeoutError, 'Process operation deadline exceeded', 'operation_timeout',
                                          'exchange' if dispatch is not None else 'receive', operation_check, 'deadline')
                try:
                    item = incoming.get_nowait()
                except queue.Empty:
                    item = None
                if item is not None:
                    if not isinstance(item, dict):
                        raise process_failure(ValueError, 'Invalid worker message', 'worker_message_invalid', 'receive', operation_check)
                    if on_message is not None and not on_message(item):
                        raise process_failure(InterruptedError, 'Worker message rejected', 'worker_message_rejected', 'receive', operation_check, 'authorization')
                    if item.get('type') == 'heartbeat':
                        exchange(item)
                    else:
                        if dispatch is not None and dispatch.is_alive():
                            raise process_failure(ValueError, 'Concurrent worker requests rejected', 'worker_concurrent_requests', 'exchange', operation_check)
                        sequence = item.get('sequence', 0)
                        operation_check = f"step:{sequence if type(sequence) is int and 0 <= sequence <= 10**12 else 0}"
                        operation_deadline = time.monotonic() + operation_timeout

                        def handle(message=item):
                            try:
                                outgoing.put_nowait((True, exchange(message)))
                            except Exception as exc:
                                outgoing.put_nowait((False, exc))

                        dispatch = threading.Thread(target=handle, daemon=True)
                        dispatch.start()
                try:
                    ok, value = outgoing.get_nowait()
                except queue.Empty:
                    pass
                else:
                    if not ok:
                        raise value
                    if time.monotonic() >= operation_deadline:
                        raise process_failure(TimeoutError, 'Process operation deadline exceeded', 'operation_timeout', 'exchange', operation_check, 'deadline')
                    send(value)
                    operation_deadline = time.monotonic() + operation_timeout
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        # A finished child's output must drain promptly even without a total investigation deadline.
        drain_deadline = min(deadline, time.monotonic() + 1)
        for thread in readers:
            while thread.is_alive() and time.monotonic() < drain_deadline:
                if cancelled is not None and cancelled.is_set():
                    raise process_failure(InterruptedError, "Process request cancelled", 'process_cancelled', 'drain', category='cancellation')
                thread.join(timeout=min(0.05, max(0, drain_deadline - time.monotonic())))
        if cancelled is not None and cancelled.is_set():
            raise process_failure(InterruptedError, "Process request cancelled", 'process_cancelled', 'drain', category='cancellation')
        if time.monotonic() >= deadline:
            raise process_failure(TimeoutError, "Process request deadline exceeded", 'process_timeout', 'execute', category='deadline')
        if any(thread.is_alive() for thread in readers):
            raise process_failure(TimeoutError, "Process descendant retained output channel", 'process_output_retained', 'drain', category='containment')
        if exceeded.is_set():
            raise process_failure(ValueError, "Process output limit exceeded" if read_failure['category'] == 'output_limit'
                                  else 'Invalid worker output channel', read_failure['code'], 'receive', category=read_failure['category'])
        if process.returncode == 72:
            raise process_failure(RuntimeError, "Process containment unavailable", 'process_containment_unavailable', 'containment', category='containment')
        return {"returncode": process.returncode, "stdout": bytes(buffers["stdout"]),
                "stderr": bytes(buffers["stderr"]), "pid": process.pid}
    finally:
        if exchange is not None and cancelled is not None:
            cancelled.set()
        if job:
            kernel, handle = job
            kernel.CloseHandle(handle)
        elif sys.platform != "win32":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()
        for thread in readers:
            thread.join(timeout=1)
        if dispatch is not None:
            dispatch.join(timeout=10)
            if dispatch.is_alive():
                raise process_failure(RuntimeError, 'Process broker cleanup pending', 'process_cleanup_pending', 'cleanup', operation_check, 'containment')


def reap_owned_process(owner: dict) -> bool:
    """Recover a former API owner's fenced Linux group without trusting a PID alone."""
    if owner.get('platform') == 'win32':
        # Windows kernel jobs close when their API owner's last handle closes.
        return sys.platform == 'win32'
    if owner.get('platform') != 'linux' or sys.platform != 'linux':
        return False
    pid, cookie = owner.get('pid'), owner.get('cookie')
    if type(pid) is not int or pid < 2 or not isinstance(cookie, str) or len(cookie) != 32:
        return False
    marker = ('ATR_PROCESS_OWNER=' + cookie).encode()
    owned = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z' or int(fields[2]) != pid or int(fields[3]) != pid:
                continue
            if entry.stat().st_uid != os.getuid() or marker not in (entry / 'environ').read_bytes().split(b'\0'):
                return False
            owned.append(int(entry.name))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError):
            return False
    if owned:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True
