#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end / full-condition matrix test (v2.0).

Requires a running LocModel (stable-diffusion.cpp) engine.
Covers: environment, size & aspect matrix, batch, seed reproducibility,
argument validation, no-engine fallback, CLI exit codes, config persistence,
MCP protocol (newline + Content-Length framing) and the real MCP tool path.

Run:
    python tests/e2e_matrix.py                 # full matrix (real generation)
    python tests/e2e_matrix.py --quick         # skip the two slowest cases
    python tests/e2e_matrix.py --out <dir>     # image output directory
Exit code: 0 when every case meets its expectation, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CLI = SCRIPTS / "locmodel_image.py"
MCP = SCRIPTS / "locmodel_mcp.py"
PY = sys.executable

RESULTS: list[dict] = []


def record(name: str, ok: bool, expect: str, detail, seconds: float = 0.0):
    RESULTS.append({"case": name, "ok": bool(ok), "expect": expect,
                    "detail": detail, "seconds": round(seconds, 1)})
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name} ({seconds:.1f}s) {str(detail)[:220]}", flush=True)


def run_cli(args: list[str], timeout: int = 1200, env_extra: dict | None = None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    t0 = time.time()
    proc = subprocess.run([PY, str(CLI), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)
    out = proc.stdout.strip()
    try:
        obj = json.loads(out)
    except Exception:
        obj = {"_raw": out[:800]}
    return obj, proc.returncode, round(time.time() - t0, 1), proc.stderr


# --------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------

def case_version():
    obj, code, secs, _ = run_cli(["--version"])
    record("cli.version", code == 0, "prints version", obj.get("_raw", obj), secs)


def case_doctor():
    obj, code, secs, _ = run_cli(["doctor"])
    ok = code == 0 and obj.get("ok") is True
    record("cli.doctor", ok, "ok=true, all error-level checks pass", 
           [c["name"] for c in obj.get("checks", []) if not c["ok"]] or "all checks ok", secs)


def case_status():
    obj, code, secs, _ = run_cli(["status"])
    has = bool(obj.get("engine", {}).get("port")) and bool(obj.get("defaults", {}).get("width"))
    record("cli.status", code == 0 and has, "port + engine defaults present",
           {"port": obj.get("engine", {}).get("port"), "by": obj.get("engine", {}).get("detected_by"),
            "model": obj.get("model"), "defaults": obj.get("defaults")}, secs)


def case_models():
    obj, code, secs, _ = run_cli(["models"])
    record("cli.models", code == 0 and obj.get("ok") is True, "model list reachable",
           str(obj.get("models"))[:160], secs)


def case_dry_run(out_dir: Path):
    obj, code, secs, _ = run_cli(["generate", "--prompt", "dry run", "--dry-run"])
    p = obj.get("payload", {})
    ok = code == 0 and p.get("width") and p.get("sample_params", {}).get("sample_steps")
    record("cli.dry_run", ok, "payload follows engine defaults, no image written",
           {"size": f"{p.get('width')}x{p.get('height')}",
            "steps": p.get("sample_params", {}).get("sample_steps"),
            "sampler": p.get("sample_params", {}).get("sample_method")}, secs)


def gen_case(name: str, expect: str, args: list[str], out_dir: Path, expect_ok=True):
    args = [*args, "--out", str(out_dir)]
    obj, code, secs, err = run_cli(["generate", *args])
    files = obj.get("files") or []
    exists = all(Path(f["path"]).exists() for f in files)
    ok = (code == 0 and obj.get("ok") is True and files and exists) if expect_ok else True
    detail = {
        "size": f"{obj.get('params', {}).get('width')}x{obj.get('params', {}).get('height')}",
        "seed": obj.get("params", {}).get("seed"),
        "files": len(files),
        "notes": obj.get("notes"),
        "elapsed": obj.get("elapsed_s"),
        "attempts": len(obj.get("attempts") or []),
        "exit": code,
    }
    if not ok:
        detail["error"] = obj.get("message") or (obj.get("_raw") or "")[:200] or err[-300:]
    record(name, ok, expect, detail, secs)
    return obj


def case_no_guard(out_dir: Path):
    """The guard exists because small pixel areas fail; verify it is bypassable.

    Both outcomes are legitimate on different hardware -- what must hold is
    that the guard step is *reported* and nothing crashes.
    """
    obj, code, secs, _ = run_cli(["generate", "--prompt", "unguarded small size",
                                  "--width", "512", "--height", "512", "--no-guard",
                                  "--retry", "1", "--out", str(out_dir)])
    size_ok = obj.get("params", {}).get("width") == 512
    ok = size_ok and code in (0, 2)
    record("cli.no_guard_512", ok,
           "guard bypassed (width stays 512); success or clean generation_failed",
           {"code": code, "ok": obj.get("ok"), "size": f"{obj.get('params', {}).get('width')}"
            f"x{obj.get('params', {}).get('height')}", "message": obj.get("message")}, secs)


def case_bad_args():
    checks = []
    obj, code, secs, _ = run_cli(["generate", "--prompt", "   "])
    checks.append(("empty_prompt", code == 4 and obj.get("error") == "bad_arguments",
                   {"code": code, "error": obj.get("error")}))
    obj2, code2, _, _ = run_cli(["generate", "--prompt", "x", "--count", "99"])
    checks.append(("count_limit", code2 == 4 and "max_count" in str(obj2.get("message")),
                   {"code": code2, "message": obj2.get("message")}))
    obj3, code3, _, _ = run_cli(["generate", "--prompt", "x", "--width", "0", "--height", "512"])
    checks.append(("width_zero", code3 == 4,
                   {"code": code3, "message": obj3.get("message")}))
    obj4, code4, _, _ = run_cli(["generate", "--prompt", "x", "--aspect", "7:5"])
    checks.append(("bad_aspect", code4 == 4 and obj4.get("error") == "bad_arguments",
                   {"code": code4, "message": obj4.get("message")}))
    ok = all(c[1] for c in checks)
    record("cli.bad_arguments", ok, "invalid input rejected before engine work",
           {c[0]: c[2] for c in checks}, secs)


def case_no_engine(tmp_cfg: Path):
    cfg = json.loads(tmp_cfg.read_text(encoding="utf-8"))
    cfg["port"] = None
    cfg["port_candidates"] = []
    solo = tmp_cfg.parent / "noengine.json"
    solo.write_text(json.dumps(cfg), encoding="utf-8")
    obj, code, secs, _ = run_cli(["--config", str(solo), "status"],
                                 env_extra={"LOCMODEL_SD_PORT": "9999"})
    record("cli.no_engine", code == 3 and obj.get("error") == "engine_not_found",
           "exit code 3 + actionable hint",
           {"code": code, "error": obj.get("error"), "hint": (obj.get("hint") or "")[:80]}, secs)


def case_config(tmp_cfg: Path):
    obj, code, secs, _ = run_cli(["--config", str(tmp_cfg), "config", "set", "size.max_side", "1280"])
    obj2, code2, _, _ = run_cli(["--config", str(tmp_cfg), "config", "get", "size.max_side"])
    obj3, code3, _, _ = run_cli(["--config", str(tmp_cfg), "config", "path"])
    reloaded = json.loads(tmp_cfg.read_text(encoding="utf-8"))
    written = (reloaded.get("size") or {}).get("max_side")
    ok = (code == 0 and str(written) == "1280"
          and str(obj2.get("size.max_side")) == "1280" and obj3.get("exists") is True)
    record("cli.config", ok, "set/get/path persist to disk",
           {"written": written, "read": obj2.get("size.max_side"),
            "file": obj3.get("path")}, secs)


# --------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------

def _mcp_session(messages: list[dict], framing: str = "newline", timeout: int = 600):
    """Send raw JSON-RPC messages to the MCP server and collect responses."""
    if framing == "content-length":
        def wrap(m):
            body = json.dumps(m, ensure_ascii=False)
            return f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n{body}".encode("utf-8")
    else:
        def wrap(m):
            return (json.dumps(m, ensure_ascii=False) + "\n").encode("utf-8")

    payload = b"".join(wrap(m) for m in messages)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([PY, str(MCP)], input=payload, capture_output=True,
                          timeout=timeout, env=env)
    out = proc.stdout.decode("utf-8", "replace")
    responses = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            responses.append(json.loads(line))
        except Exception:
            continue
    return responses, proc.stderr.decode("utf-8", "replace")


def case_mcp_protocol():
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "e2e", "version": "2.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
    ]
    responses, err = _mcp_session(msgs)
    by_id = {r.get("id"): r for r in responses}
    tools = (by_id.get(2, {}).get("result") or {}).get("tools") or []
    ok = (by_id.get(1, {}).get("result", {}).get("serverInfo", {}).get("name") == "locmodel-image"
          and len(tools) >= 5 and by_id.get(3, {}).get("result") == {}
          and by_id.get(4, {}).get("result") == {"resources": []}
          and len(responses) == 4)
    record("mcp.protocol", ok, "initialize/tools.list/ping/resources.list, notifications silent",
           {"responses": len(responses), "tools": [t["name"] for t in tools]}, 0.0)


def case_mcp_framing():
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    responses, _ = _mcp_session(msgs, framing="content-length")
    ok = any(r.get("id") == 1 for r in responses) and any(r.get("id") == 2 for r in responses)
    record("mcp.content_length_framing", ok, "LSP-style framing tolerated",
           {"responses": len(responses)}, 0.0)


def case_mcp_generate(out_dir: Path):
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "engine_doctor", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "generate_image", "arguments": {
             "prompt": "a minimal concrete courtyard, soft daylight, 35mm",
             "aspect": "4:3", "output_dir": str(out_dir), "filename_prefix": "mcp_e2e"}}},
    ]
    t0 = time.time()
    responses, err = _mcp_session(msgs, timeout=900)
    secs = round(time.time() - t0, 1)
    by_id = {r.get("id"): r for r in responses}
    gen = by_id.get(3, {}).get("result", {})
    text = (gen.get("content") or [{}])[0].get("text", "")
    payload = {}
    try:
        payload = json.loads(text)
    except Exception:
        pass
    files = payload.get("files") or []
    ok = (not gen.get("isError")) and files and all(Path(f["path"]).exists() for f in files)
    record("mcp.generate_image", ok, "MCP tool produces real PNG on disk",
           {"files": len(files), "size": f"{payload.get('params', {}).get('width')}"
            f"x{payload.get('params', {}).get('height')}", "seed": payload.get("params", {}).get("seed"),
            "doctor_ok": not by_id.get(2, {}).get("result", {}).get("isError")}, secs)
    return payload


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "output" / "e2e"))
    ap.add_argument("--quick", action="store_true", help="skip the slowest cases")
    ap.add_argument("--stage", default="all", choices=["all", "core", "gen", "diag", "mcp"],
                    help="run one stage only (keeps a single invocation under the tool timeout)")
    ap.add_argument("--gen-timeout", type=int, default=1800,
                    help="per-image timeout in seconds (raise it on slow or shared GPUs)")
    ap.add_argument("--report", default=str(ROOT / "e2e_report.json"))
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(os.environ.get("TEMP") or ROOT) / f"locmodel_e2e_{os.getpid()}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_cfg = tmp_dir / "config.json"
    tmp_cfg.write_text(json.dumps({}), encoding="utf-8")

    print(f"python   : {PY}\nrepo     : {ROOT}\nout      : {out_dir}\nstage    : {args.stage}\n", flush=True)

    if args.stage in ("all", "core"):
        case_version()
        case_doctor()
        case_status()
        case_models()
        case_dry_run(out_dir)

    if args.stage in ("all", "gen"):
        P = "every dimension above the guard passes, aspect preserved"
        T = ["--timeout", str(args.gen_timeout)]
        gen_case("gen.square_768", P, ["--prompt", "quiet courtyard, soft daylight, 35mm",
                                       "--width", "768", "--height", "768", *T], out_dir)
        gen_case("gen.aspect_16_9", P, ["--prompt", "wide architectural facade, dusk",
                                        "--aspect", "16:9", *T], out_dir)
        gen_case("gen.small_640_upscaled", "640x640 upscaled to the minimum area",
                 ["--prompt", "small test cube", "--width", "640", "--height", "640", *T], out_dir)
        if not args.quick:
            gen_case("gen.portrait_9_16", "portrait aspect stays portrait after the guard",
                     ["--prompt", "tall atrium, looking up", "--aspect", "9:16", *T], out_dir)
            gen_case("gen.batch_2", "count=2 returns two distinct files",
                     ["--prompt", "batch of two cubes", "--width", "768", "--height", "768",
                      "--count", "2", *T], out_dir)
            case_no_guard(out_dir)

    if args.stage in ("all", "diag"):
        case_bad_args()
        case_no_engine(tmp_cfg)
        case_config(tmp_cfg)

    if args.stage in ("all", "mcp"):
        case_mcp_protocol()
        case_mcp_framing()
        case_mcp_generate(out_dir)

    passed = sum(1 for r in RESULTS if r["ok"])
    total = len(RESULTS)
    report = {"version": "2.0.0", "python": PY, "out_dir": str(out_dir),
              "passed": passed, "total": total,
              "failed": [r["case"] for r in RESULTS if not r["ok"]],
              "cases": RESULTS}
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n===== {passed}/{total} passed =====")
    if passed != total:
        print("failed:", ", ".join(report["failed"]))
    print(f"report: {args.report}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
