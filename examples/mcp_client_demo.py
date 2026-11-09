#!/usr/bin/env python3
"""Minimal MCP client: spawn the server, handshake, list tools, call one.

Demonstrates the stdio contract so you can wire the server into any host that
does not have a ready-made MCP client, or debug a host that misbehaves.

Usage:
    python examples/mcp_client_demo.py            # handshake + list tools only
    python examples/mcp_client_demo.py --generate # also generate one image
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "scripts" / "locmodel_mcp.py"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--prompt", default="single cube on a concrete floor, soft light")
    args = ap.parse_args()

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.Popen([sys.executable, str(SERVER)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", env=env)
    _id = 0

    def call(method, params=None):
        nonlocal _id
        _id += 1
        msg = {"jsonrpc": "2.0", "id": _id, "method": method}
        if params is not None:
            msg["params"] = params
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            if not line:
                raise SystemExit("server closed the stream; check stderr")
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue                      # non-protocol noise
            if resp.get("id") == _id:
                return resp

    try:
        init = call("initialize", {"protocolVersion": "2025-06-18",
                                   "capabilities": {},
                                   "clientInfo": {"name": "demo", "version": "0.1"}})
        print("protocol :", init["result"]["protocolVersion"])
        print("server   :", json.dumps(init["result"]["serverInfo"], ensure_ascii=False))

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

        tools = call("tools/list")["result"]["tools"]
        print(f"tools    : {len(tools)}")
        for t in tools:
            print(f"  - {t['name']}: {t.get('description', '')[:70]}")

        status = call("tools/call", {"name": "locmodel_status", "arguments": {}})["result"]
        body = status.get("content", [{}])[0].get("text", "")
        print("\nlocmodel_status ->", body[:300])

        if args.generate:
            res = call("tools/call", {"name": "generate_image",
                                      "arguments": {"prompt": args.prompt, "aspect": "1:1"}})["result"]
            text = res.get("content", [{}])[0].get("text", "")
            print("\ngenerate_image ->", text[:400])
            if res.get("isError"):
                return 2
        return 0
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
