"""Linux child fence: descendants cannot detach from the supervised process group."""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import sys


def contain_group():
    library = ctypes.util.find_library("seccomp")
    if not library:
        raise RuntimeError("Linux containment dependency unavailable")
    seccomp = ctypes.CDLL(library, use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]

    class Comparison(ctypes.Structure):
        _fields_ = [("argument", ctypes.c_uint), ("operation", ctypes.c_int),
                    ("mask", ctypes.c_uint64), ("value", ctypes.c_uint64)]

    seccomp.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                              ctypes.c_uint, ctypes.POINTER(Comparison)]
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS.
        raise RuntimeError("Linux containment unavailable")
    context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW.
    if not context:
        raise RuntimeError("Linux containment unavailable")
    try:
        for name in ("setsid", "setpgid", "unshare", "setns", "clone3"):
            syscall = seccomp.seccomp_syscall_resolve_name(name.encode())
            error = errno.ENOSYS if name == "clone3" else errno.EPERM
            if syscall < 0 or seccomp.seccomp_rule_add(context, 0x00050000 | error, syscall, 0) != 0:
                raise RuntimeError("Linux containment unavailable")
        clone = seccomp.seccomp_syscall_resolve_name(b"clone")
        for flag in (0x20000000, 0x10000000):  # CLONE_NEWPID and CLONE_NEWUSER.
            comparison = Comparison(0, 7, flag, flag)  # SCMP_CMP_MASKED_EQ.
            if clone < 0 or seccomp.seccomp_rule_add_array(context, 0x00050000 | errno.EPERM,
                                                         clone, 1, ctypes.byref(comparison)) != 0:
                raise RuntimeError("Linux containment unavailable")
        if seccomp.seccomp_load(context) != 0:
            raise RuntimeError("Linux containment unavailable")
    finally:
        seccomp.seccomp_release(context)


def main():
    try:
        contain_group()
    except Exception:
        sys.stderr.write("process_containment_unavailable\n")
        raise SystemExit(72) from None
    if len(sys.argv) < 2:
        raise SystemExit(72)
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)


if __name__ == "__main__":
    main()
