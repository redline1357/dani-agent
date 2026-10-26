"""Harmless Windows lifecycle fixtures, not a filesystem/network sandbox."""
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

k = c.WinDLL("kernel32", use_last_error=True)
SIZE = c.c_size_t


class STARTUP(c.Structure):
    _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR), ("title", w.LPWSTR),
                ("x", w.DWORD), ("y", w.DWORD), ("xs", w.DWORD), ("ys", w.DWORD),
                ("xc", w.DWORD), ("yc", w.DWORD), ("fill", w.DWORD), ("flags", w.DWORD),
                ("show", w.WORD), ("reserved2size", w.WORD), ("reserved2", c.c_void_p),
                ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]


class PROCESS(c.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


class BASIC(c.Structure):
    _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64), ("flags", w.DWORD),
                ("minws", SIZE), ("maxws", SIZE), ("active", w.DWORD), ("affinity", SIZE),
                ("priority", w.DWORD), ("scheduling", w.DWORD)]


class IO(c.Structure):
    _fields_ = [(n, c.c_uint64) for n in ("ro", "wo", "oo", "rt", "wt", "ot")]


class LIMITS(c.Structure):
    _fields_ = [("basic", BASIC), ("io", IO), ("process_mem", SIZE), ("job_mem", SIZE),
                ("peak_process", SIZE), ("peak_job", SIZE)]


class PIDS(c.Structure):
    _fields_ = [("assigned", w.DWORD), ("listed", w.DWORD), ("pids", SIZE * 16)]


def api(name, args, result):
    function = getattr(k, name)
    function.argtypes, function.restype = args, result
    return function


createjob = api("CreateJobObjectW", [c.c_void_p, w.LPCWSTR], w.HANDLE)
setjob = api("SetInformationJobObject", [w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL)
queryjob = api("QueryInformationJobObject", [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p], w.BOOL)
assign = api("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL)
create = api("CreateProcessW", [w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD,
                              c.c_void_p, w.LPCWSTR, c.POINTER(STARTUP), c.POINTER(PROCESS)], w.BOOL)
resume = api("ResumeThread", [w.HANDLE], w.DWORD)
close = api("CloseHandle", [w.HANDLE], w.BOOL)
wait = api("WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD)
openprocess = api("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
terminatejob = api("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL)
terminateprocess = api("TerminateProcess", [w.HANDLE, w.UINT], w.BOOL)
event = api("CreateEventW", [c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE)
setevent = api("SetEvent", [w.HANDLE], w.BOOL)
exitcode = api("GetExitCodeProcess", [w.HANDLE, c.POINTER(w.DWORD)], w.BOOL)


def check(value):
    if not value:
        raise c.WinError(c.get_last_error())
    return value


CHILD = """
import ctypes, subprocess, sys, time
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.OpenEventW.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_wchar_p]
k.OpenEventW.restype=ctypes.c_void_p
k.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
k.CloseHandle.argtypes=[ctypes.c_void_p]
h=k.OpenEventW(0x100000,False,sys.argv[1])
if not h: raise SystemExit(10)
p=subprocess.Popen([sys.executable,'-I','-B','-c','import time; time.sleep(20)'])
try:
    until=time.monotonic()+15
    while time.monotonic()<until:
        if k.WaitForSingleObject(h,50)==0: break
finally:
    if p.poll() is None: p.terminate()
    p.wait(timeout=3)
    k.CloseHandle(h)
"""


def case(mode):
    job = signal = None
    pi = PROCESS()
    observed = []
    started = time.monotonic()
    try:
        name = "Local\\DaniPhase0_" + uuid.uuid4().hex
        signal = check(event(None, True, False, name))
        job = check(createjob(None, None))
        limits = LIMITS()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway
        check(setjob(job, 9, c.byref(limits), c.sizeof(limits)))
        command = c.create_unicode_buffer(subprocess.list2cmdline([sys.executable, "-I", "-B", "-c", CHILD, name]))
        si = STARTUP()
        si.cb = c.sizeof(si)
        check(create(sys.executable, command, None, None, False, 0x4 | 0x08000000,
                     None, str(Path(__file__).parent), c.byref(si), c.byref(pi)))
        check(assign(job, pi.process))
        if resume(pi.thread) == 0xFFFFFFFF:
            raise c.WinError(c.get_last_error())
        until = time.monotonic() + 4
        while time.monotonic() < until:
            pids = PIDS()
            check(queryjob(job, 3, c.byref(pids), c.sizeof(pids), None))
            if pids.listed == 2:
                break
            time.sleep(.05)
        if pids.listed != 2:
            raise RuntimeError("expected parent and grandchild in job")
        for pid in pids.pids[:pids.listed]:
            observed.append(check(openprocess(0x100000, False, pid)))
        alive = all(wait(handle, 0) == 258 for handle in observed)
        if mode == "kill_on_close":
            check(close(job))
            job = None
        elif mode == "timeout":
            if wait(pi.process, 300) != 258:
                raise RuntimeError("fixture exited before timeout")
            check(terminatejob(job, 124))
        else:
            check(setevent(signal))  # cooperative cancellation, not a CLI console signal
        stopped = all(wait(handle, 5000) == 0 for handle in observed)
        code = w.DWORD()
        check(exitcode(pi.process, c.byref(code)))
        return {"case": mode, "created_suspended": True, "assigned_before_resume": True,
                "observed_processes": int(pids.listed), "both_alive": alive,
                "all_observed_processes_stopped": stopped, "parent_exit": int(code.value),
                "elapsed_seconds": round(time.monotonic()-started, 3),
                "pass": alive and stopped and (mode != "cooperative_interrupt" or code.value == 0)}
    finally:
        if job:
            terminatejob(job, 125)
            close(job)
        # Covers failure before job assignment; no uncontained suspended process remains.
        if pi.process and wait(pi.process, 0) == 258:
            terminateprocess(pi.process, 125)
            wait(pi.process, 5000)
        for handle in observed + [pi.thread, pi.process, signal]:
            if handle:
                close(handle)


if __name__ == "__main__":
    results = [case(mode) for mode in ("kill_on_close", "timeout", "cooperative_interrupt")]
    output = {"model_calls": 0, "results": results,
              "scope": "Self-authored Python fixtures only; not actual CLI cancellation or session recovery."}
    Path(__file__).with_name("process_results.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, indent=2))
    raise SystemExit(0 if all(r["pass"] for r in results) else 1)
