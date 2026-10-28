"""Bridge to the PCB engine of mixelpixx/KiCAD-MCP-Server.

That project's Python backend (python/kicad_interface.py) runs inside KiCad's own
Python, where the pcbnew module is available, and reads one JSON command per line
on stdin: {"command": ..., "params": {...}, "requestId": n}. It answers with one
JSON line that echoes "_requestId". We start it on first use and talk to it
directly, without the project's Node.js layer.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

READY_TIMEOUT_S = 120


class BackendError(RuntimeError):
    pass


class KicadBackend:
    def __init__(self, python_exe: Path, script: Path, env: dict[str, str] | None = None,
                 log_file: Path | None = None):
        self.python_exe = python_exe
        self.script = script
        self.env = env or {}
        self.log_file = log_file
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # -- process management -------------------------------------------------

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start(self) -> None:
        if not self.script.exists():
            raise BackendError(
                f"PCB backend not found at {self.script}. Clone mixelpixx/KiCAD-MCP-Server next to "
                "kicad-mcp or set KICAD_MCP_BACKEND to its python/kicad_interface.py."
            )
        stderr = subprocess.DEVNULL
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            stderr = open(self.log_file, "a", encoding="utf-8")
        self._lines = queue.Queue()
        self._proc = subprocess.Popen(
            [str(self.python_exe), str(self.script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            cwd=str(self.script.parent),
            env={**os.environ, "PYTHONUNBUFFERED": "1", **self.env},
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._pump, args=(self._proc, self._lines), daemon=True).start()

        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            line = self._next_line(deadline, "start")
            try:
                if json.loads(line).get("type") == "ready":
                    return
            except (ValueError, AttributeError):
                continue  # pcbnew prints non-JSON noise before READY

    @staticmethod
    def _pump(proc: subprocess.Popen, lines: queue.Queue) -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def _next_line(self, deadline: float, what: str) -> str:
        remaining = deadline - time.monotonic()
        try:
            line = self._lines.get(timeout=max(remaining, 0.01))
        except queue.Empty:
            self.close()
            raise BackendError(f"PCB backend timed out during '{what}'") from None
        if line is None:
            self.close()
            raise BackendError(f"PCB backend exited during '{what}' (see {self.log_file})")
        return line

    def close(self) -> None:
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.kill()
            self._proc = None

    # -- requests ---------------------------------------------------------------

    def call(self, command: str, params: dict | None = None, timeout_s: float = 300) -> dict:
        """Send one command and return the backend's JSON answer (without _requestId)."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        with self._lock:
            if not self._alive():
                self._start()
            request_id = next(self._ids)
            self._proc.stdin.write(json.dumps({"command": command, "params": params,
                                               "requestId": request_id}) + "\n")
            self._proc.stdin.flush()
            deadline = time.monotonic() + timeout_s
            while True:
                line = self._next_line(deadline, command)
                try:
                    result = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(result, dict) or result.get("_requestId") != request_id:
                    continue  # stray output or a late answer to an older request
                result.pop("_requestId", None)
                return result
