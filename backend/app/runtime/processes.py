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


def run_bounded(arguments: list[str], request: bytes, *, timeout: float, max_output: int = 2 * 1024 * 1024,
                cancelled: threading.Event | None = None, on_spawn=None, exchange=None, on_message=None) -> dict:
    if timeout <= 0 or len(request) > 2 * 1024 * 1024:
        raise ValueError("Invalid process request bounds")
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError("Process request cancelled")
    deadline = time.monotonic() + timeout
    if sys.platform == "linux":
        arguments = [sys.executable, "-m", "app.runtime.process_bootstrap", *arguments]
    owner_cookie = uuid.uuid4().hex
    environment = {**child_environment(), 'ATR_PROCESS_OWNER': owner_cookie}
    process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=environment,
                               start_new_session=sys.platform != "win32")
    job = None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    exceeded = threading.Event()
    readers = []
    buffer_lock = threading.Lock()
    incoming, outgoing = queue.Queue(maxsize=64), queue.Queue(maxsize=2)
    dispatch = None
    write_lock = threading.Lock()

    def send(value):
        encoded = json.dumps(value, ensure_ascii=False).encode() + b'\n'
        if len(encoded) > max_output:
            raise ValueError('Process reply limit exceeded')
        with write_lock:
            process.stdin.write(encoded)
            process.stdin.flush()

    def read(stream, name):
        pending = bytearray()
        try:
            while chunk := stream.read1(4096):
                with buffer_lock:
                    if sum(len(value) for value in buffers.values()) + len(chunk) > max_output:
                        exceeded.set()
                        break
                    buffers[name].extend(chunk)
                if exchange is not None and name == 'stdout':
                    pending.extend(chunk)
                    while b'\n' in pending:
                        line, _, pending = pending.partition(b'\n')
                        incoming.put_nowait(json.loads(line))
                    if len(pending) > 65536:
                        exceeded.set()
                        break
            if exchange is not None and pending:
                exceeded.set()
        except (OSError, ValueError, queue.Full):
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
            job = _windows_job(process)
        if on_spawn is not None:
            on_spawn({'platform': sys.platform, 'pid': process.pid, 'cookie': owner_cookie})
        if (cancelled is not None and cancelled.is_set()) or time.monotonic() >= deadline:
            raise InterruptedError("Process dispatch no longer authorized")
        for name in buffers:
            thread = threading.Thread(target=read, args=(getattr(process, name), name), daemon=True)
            thread.start()
            readers.append(thread)
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        while process.poll() is None:
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("Process request cancelled")
            if exceeded.is_set():
                raise ValueError("Process output limit exceeded")
            if time.monotonic() >= deadline:
                raise TimeoutError("Process request deadline exceeded")
            if exchange is not None:
                try:
                    item = incoming.get_nowait()
                except queue.Empty:
                    item = None
                if item is not None:
                    if not isinstance(item, dict):
                        raise ValueError('Invalid worker message')
                    if on_message is not None and not on_message(item):
                        raise InterruptedError('Worker message rejected')
                    if item.get('type') == 'heartbeat':
                        exchange(item)
                    else:
                        if dispatch is not None and dispatch.is_alive():
                            raise ValueError('Concurrent worker requests rejected')

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
                    send(value)
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        for thread in readers:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError("Process request cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Process request deadline exceeded")
        if any(thread.is_alive() for thread in readers):
            raise TimeoutError("Process descendant retained output channel")
        if exceeded.is_set():
            raise ValueError("Process output limit exceeded")
        if process.returncode == 72:
            raise RuntimeError("Process containment unavailable")
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
                raise RuntimeError('Process broker cleanup pending')


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
