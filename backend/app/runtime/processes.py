"""Deadline-enforced process trees. Children must await their private stdin request."""
from __future__ import annotations

import os
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
                cancelled: threading.Event | None = None, on_spawn=None) -> dict:
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

    def read(stream, name):
        try:
            while chunk := stream.read(4096):
                with buffer_lock:
                    if sum(len(value) for value in buffers.values()) + len(chunk) > max_output:
                        exceeded.set()
                        break
                    buffers[name].extend(chunk)
        except (OSError, ValueError):
            exceeded.set()

    def write():
        try:
            if (cancelled is not None and cancelled.is_set()) or time.monotonic() >= deadline:
                process.stdin.close()
                return
            process.stdin.write(request)
            process.stdin.close()
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
