"""Managed process groups with identity checks before signalling."""
import hashlib
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


class DarwinBSDInfo(ctypes.Structure):
    # Apple bsd/sys/proc_info.h: PROC_PIDTBSDINFO, public ABI.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'jobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('start_seconds', ctypes.c_uint64), ('start_microseconds', ctypes.c_uint64)]


def darwin_identity(pid):
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    function = library.proc_pidinfo
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    info = DarwinBSDInfo()
    count = function(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if count != ctypes.sizeof(info) or info.pid != pid or info.status == 5:  # SZOMB
        return None
    # Birth survives exec; argv does not. Microseconds protect against PID reuse.
    return dict(pid=pid, pgid=info.pgid, birth=f'{info.start_seconds}:{info.start_microseconds}')


def identity(pid):
    if type(pid) is not int or pid <= 1:
        return None
    try:
        if sys.platform == 'darwin':
            return darwin_identity(pid)
        proc = Path(f'/proc/{pid}/stat')
        if proc.exists():
            fields = proc.read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z':
                return None
            return dict(pid=pid, pgid=int(fields[2]), birth=fields[19],
                        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat=', '-o', 'lstart=',
                                 '-o', 'pgid=', '-o', 'command='], capture_output=True, text=True, timeout=3)
        fields = result.stdout.strip().split(None, 7)
        if result.returncode or len(fields) != 8 or fields[0].startswith('Z'):
            return None
        return dict(pid=pid, pgid=int(fields[6]), birth=' '.join(fields[1:6]),
                    command_sha256=hashlib.sha256(fields[7].encode()).hexdigest())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def matches(record):
    return bool(record) and identity(record['pid']) == record


def group_alive(pgid):
    result = subprocess.run(['ps', '-eo', 'pgid=', '-o', 'stat='], capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise ValueError('Cannot inspect process group')
    return any(len(parts := line.split()) == 2 and parts[0] == str(pgid) and not parts[1].startswith('Z')
               for line in result.stdout.splitlines())


def stop_group(worker, child=None, grace_seconds=1):
    proof = worker if matches(worker) else child if matches(child) else None
    pgid = (worker or {}).get('pgid')
    if not pgid or pgid <= 1 or pgid == os.getpgrp():
        raise ValueError('Refusing to signal an unowned process group')
    if not group_alive(pgid):
        return True
    if proof is None or proof['pgid'] != pgid:
        raise ValueError('Process identity uncertain; reconcile before signalling')
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not group_alive(pgid):
            return True
        time.sleep(.05)
    # Re-check identity; never signal a group whose identity has become ambiguous.
    if group_alive(pgid):
        if not matches(proof):
            raise ValueError('Group still active without an identifiable owner; quarantined')
        os.killpg(pgid, signal.SIGKILL)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not group_alive(pgid):
            return True
        time.sleep(.05)
    return not group_alive(pgid)
