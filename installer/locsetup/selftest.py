"""Self-tests that prove the thing actually works, not that files exist.

The load-bearing test is ``mcp_smoke``: it launches the *installed* entry point
as a real MCP server over stdio, performs the same initialize/tools/list exchange
a host would, and (optionally) calls engine_doctor. If that passes, an agent
client will see the tools -- which is the only definition of "deployed" that matters.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from pathlib import Path


def _reader(stream, q: "queue.Queue") -> None:
    try:
        for line in iter(stream.readline, b""):
            if line:
                q.put(line)
    finally:
        q.put(None)


def _pump(stream, q: "queue.Queue") -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            q.put(chunk)
    finally:
        pass


class McpSession:
    """Tiny MCP stdio client, used only by the self-test."""

    def __init__(self, command: list[str], cwd: Path | None = None,
                 env: dict | None = None, timeout: float = 30.0):
        self.timeout = timeout
        self.stderr_log: list[str] = []
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env={**os.environ, **(env or {})},
            bufsize=0,
        )
        self._out: "queue.Queue" = queue.Queue()
        self._err: "queue.Queue" = queue.Queue()
        threading.Thread(target=_reader, args=(self.proc.stdout, self._out), daemon=True).start()
        threading.Thread(target=_pump, args=(self.proc.stderr, self._err), daemon=True).start()
        self._id = 0

    def _drain_stderr(self) -> None:
        while True:
            try:
                chunk = self._err.get_nowait()
            except queue.Empty:
                break
            if chunk:
                self.stderr_log.append(chunk.decode("utf-8", "replace"))

    def send(self, method: str, params: dict | None = None, *, notify: bool = False) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        payload = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
        assert self.proc.stdin is not None
        self.proc.stdin.write(payload)
        self.proc.stdin.flush()

    def recv(self, timeout: float | None = None) -> dict:
        deadline = self.timeout if timeout is None else timeout
        try:
            line = self._out.get(timeout=deadline)
        except queue.Empty:
            self._drain_stderr()
            raise TimeoutError(f"no response within {deadline}s")
        if line is None:
            self._drain_stderr()
            raise RuntimeError("server closed stdout (crashed?)")
        text = line.decode("utf-8", "replace").strip()
        if not text:
            return self.recv(deadline)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"non-JSON on stdout: {text[:200]!r}") from exc

    def call(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        self.send(method, params)
        return self.recv(timeout)

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
        self._drain_stderr()
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass


def mcp_smoke(command: list[str], cwd: Path | None = None, env: dict | None = None,
              *, timeout: float = 45.0, expect_tools: tuple[str, ...] = ("generate_image",),
              call_doctor: bool = False) -> dict:
    """Handshake + tools/list (+ optional engine_doctor). Returns a check record."""
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, level: str = "error"):
        checks.append({"name": name, "ok": bool(ok), "level": level, "detail": detail})

    sess = None
    try:
        sess = McpSession(command, cwd=cwd, env=env, timeout=timeout)

        init = sess.call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "locmodel-image-setup", "version": "3.0.0"},
        })
        if "error" in init:
            add("mcp.initialize", False, json.dumps(init["error"], ensure_ascii=False))
            return {"ok": False, "checks": checks}
        info = (init.get("result") or {}).get("serverInfo") or {}
        add("mcp.initialize", bool(info.get("name")),
            f"serverInfo={info.get('name')} v{info.get('version')} "
            f"protocol={(init.get('result') or {}).get('protocolVersion')}")

        sess.send("notifications/initialized", {}, notify=True)

        listing = sess.call("tools/list", {})
        tools = [t.get("name") for t in ((listing.get("result") or {}).get("tools") or [])]
        missing = [t for t in expect_tools if t not in tools]
        add("mcp.tools_list", not missing,
            f"tools={', '.join(tools) or 'none'}" + (f"; missing {missing}" if missing else ""))

        if call_doctor:
            res = sess.call("tools/call", {"name": "engine_doctor", "arguments": {}}, timeout=timeout)
            content = (res.get("result") or {}).get("content") or []
            text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            try:
                doc = json.loads(text)
                engine_ok = bool(doc.get("ok"))
                detail = "; ".join(
                    f"{c['name']}={'ok' if c['ok'] else 'FAIL'}"
                    for c in doc.get("checks", []) if c.get("level") == "error"
                ) or "no error-level checks"
            except Exception:
                engine_ok = not (res.get("result") or {}).get("isError")
                detail = text[:300]
            add("engine.doctor", engine_ok, detail, "warn")
    except Exception as exc:
        add("mcp.handshake", False, f"{type(exc).__name__}: {exc}")
    finally:
        if sess is not None:
            sess.close()
            if sess.stderr_log:
                tail = "".join(sess.stderr_log).strip().splitlines()[-6:]
                checks.append({"name": "mcp.stderr", "ok": True, "level": "info",
                               "detail": " | ".join(tail)})

    ok = all(c["ok"] for c in checks if c["level"] == "error")
    return {"ok": ok, "checks": checks}
