from __future__ import annotations

import os
import subprocess
import sys
import time

_CALLS = 0


async def fast_fact(query: str, child_pid_path: str | None = None):
    global _CALLS
    _CALLS += 1
    if query == "block":
        time.sleep(60)
    if query == "spawn-child":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if child_pid_path:
            with open(child_pid_path, "w", encoding="ascii") as stream:
                stream.write(str(child.pid))
        time.sleep(60)
    if query == "spawn-child-return":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if child_pid_path:
            with open(child_pid_path, "w", encoding="ascii") as stream:
                stream.write(str(child.pid))
        return {"query": query, "child_pid": child.pid, "worker_pid": os.getpid()}
    if query == "spawn-detached-child":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if child_pid_path:
            with open(child_pid_path, "w", encoding="ascii") as stream:
                stream.write(str(child.pid))
        time.sleep(60)
    if query == "spawn-detached-return":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if child_pid_path:
            with open(child_pid_path, "w", encoding="ascii") as stream:
                stream.write(str(child.pid))
        return {"query": query, "child_pid": child.pid, "worker_pid": os.getpid()}
    if query == "spawn-double-fork-return":
        if os.name == "nt":
            raise RuntimeError("double-fork fixture is POSIX-only")
        script = (
            "import os,sys,time\nfrom pathlib import Path\n"
            "if os.fork(): os._exit(0)\n"
            "if os.fork(): os._exit(0)\n"
            "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii')\n"
            "time.sleep(60)\n"
        )
        intermediate = subprocess.Popen(
            [sys.executable, "-c", script, str(child_pid_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        intermediate.wait(timeout=3)
        deadline = time.monotonic() + 3
        while child_pid_path and not os.path.exists(child_pid_path):
            if time.monotonic() >= deadline:
                raise RuntimeError("double-fork child did not publish its PID")
            time.sleep(0.01)
        return {"query": query, "worker_pid": os.getpid()}
    if query == "stdio-noise":
        os.write(1, b"not-a-protocol-frame\n")
        os.write(2, b"stderr-noise\n")
        return {
            "query": query,
            "calls": _CALLS,
            "worker_pid": os.getpid(),
            "stdin_eof": os.read(0, 1) == b"",
        }
    return {"query": query, "calls": _CALLS, "worker_pid": os.getpid()}


async def deep_research(query: str, sources: int = 3):
    return {"query": query, "sources": sources, "worker_pid": os.getpid()}


async def aggressive_shopping(query: str, currency: str = "USD"):
    return [{"query": query, "currency": currency, "worker_pid": os.getpid()}]
