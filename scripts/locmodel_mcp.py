#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LocModel Image MCP Server  (v2.0.0)

A dependency-free Model Context Protocol server (stdio transport) that exposes
the local stable-diffusion.cpp / LocModel image engine to MCP hosts
(Marvis, Claude Desktop, Cline, Continue, custom agents ...).

Protocol notes
--------------
* JSON-RPC 2.0 over stdio, newline-delimited (MCP spec). For robustness the
  reader also accepts LSP-style ``Content-Length`` framing used by some hosts.
* stdout carries protocol traffic ONLY; all logging goes to stderr.
* Implements: initialize, notifications/initialized, ping, tools/list,
  tools/call, resources/list, resources/templates/list, prompts/list.
* No third-party packages; Python >= 3.8.

Tools
-----
generate_image  generate 1-N PNG files locally and return absolute paths
locmodel_status engine port, model, engine defaults, VRAM, size policy
list_models     models reported by the engine
engine_doctor   environment / engine / output self-check
engine_discover force port re-discovery (after restarting the engine)
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import locmodel_image as core  # noqa: E402
except Exception as _exc:  # pragma: no cover - surfaced through tool errors
    core = None
    _CORE_IMPORT_ERROR = _exc
else:
    _CORE_IMPORT_ERROR = None

SERVER_NAME = "locmodel-image"
SERVER_VERSION = core.VERSION if core else "2.0.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2024-11-05"


def log(msg: str) -> None:
    sys.stderr.write(f"[locmodel-mcp] {msg}\n")
    sys.stderr.flush()


def _policy_text() -> str:
    """Render size policy from the single source of truth (core.DEFAULTS)."""
    if not core:
        return "size guard: 768px minimum area, 1536px maximum edge"
    cfg = core.load_config()
    pol = core._size_policy(cfg, None)
    return (f"default {core.DEFAULTS['size']['min_area'] // 1024}k-pixel minimum area, "
            f"long edge <= {pol['max_side']}px, multiple of {pol['multiple']}")


def _generate_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Positive prompt. English is strongly recommended; include "
                               "subject, environment, lighting, camera/lens and style.",
            },
            "negative_prompt": {
                "type": "string",
                "description": "What to avoid. A quality-oriented default is applied when omitted.",
            },
            "width": {"type": "integer", "description": "Pixel width. Defaults to the engine's own default."},
            "height": {"type": "integer", "description": "Pixel height. Defaults to the engine's own default."},
            "aspect": {
                "type": "string",
                "enum": list(core.ASPECTS) if core else ["1:1", "16:9", "9:16", "4:3", "3:4"],
                "description": "Aspect preset used when width/height are omitted.",
            },
            "steps": {"type": "integer", "description": "Sampling steps. Defaults to the engine's own default."},
            "sampler": {"type": "string", "description": "Sampler name, e.g. euler / dpm++2m."},
            "seed": {"type": "integer", "description": "-1 for random; a fixed seed reproduces an image."},
            "count": {"type": "integer", "description": "Number of images (1-4 recommended)."},
            "output_dir": {
                "type": "string",
                "description": "Absolute directory for the PNG files. Omit to use the working "
                               "directory's ./output folder.",
            },
            "filename_prefix": {"type": "string", "description": "Semantic filename prefix, e.g. architecture_court."},
            "model": {"type": "string", "description": "Model override for multi-model engines."},
            "timeout_s": {"type": "integer", "description": "Job timeout in seconds."},
        },
        "required": ["prompt"],
    }


TOOLS = [
    {
        "name": "generate_image",
        "description": (
            "Generate images with the LOCAL LocModel / stable-diffusion.cpp engine "
            "(offline, no cloud cost). Pass an already-optimised English prompt; the tool "
            "returns absolute PNG paths plus the seed used for reproduction. "
            f"Sizing is guarded automatically ({_policy_text()}); one 1024x1024 image takes "
            "roughly 20-60 s on a mid-range GPU."
        ),
        "inputSchema": _generate_schema(),
    },
    {
        "name": "locmodel_status",
        "description": "Inspect the local engine: port and discovery source, loaded model, "
                       "engine-reported generation defaults, VRAM and the active size policy.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_models",
        "description": "List the models the local engine reports (A1111-compatible /sdapi/v1/sd-models).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "engine_doctor",
        "description": "Self-check: python, engine reachability, model, VRAM vendor, output "
                       "directory writability, available helper tools. Use this first when "
                       "generation fails.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "engine_discover",
        "description": "Force engine port re-discovery (use after restarting LocModel, which "
                       "may pick a different port).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _text(payload) -> dict:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": body}]}


def _error(message: str, hint: str = "") -> dict:
    text = message if not hint else f"{message}\n\nHint: {hint}"
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _require_core() -> str | None:
    if core is None:
        return (f"Core module locmodel_image.py could not be imported: {_CORE_IMPORT_ERROR}. "
                "Keep locmodel_mcp.py and locmodel_image.py in the same folder.")
    return None


def call_tool(name: str, args: dict) -> dict:
    missing = _require_core()
    if missing:
        return _error(missing)

    cfg = core.load_config()

    if name == "generate_image":
        prompt = (args or {}).get("prompt") or ""
        if not prompt.strip():
            return _error("Missing required argument: prompt.")
        try:
            res = core.generate(
                prompt,
                cfg,
                negative_prompt=args.get("negative_prompt", ""),
                width=args.get("width"),
                height=args.get("height"),
                aspect=args.get("aspect"),
                steps=args.get("steps"),
                sampler=args.get("sampler"),
                seed=int(args.get("seed", -1)),
                count=int(args.get("count", 1)),
                out_dir=args.get("output_dir"),
                filename_prefix=args.get("filename_prefix") or "locmodel",
                timeout=args.get("timeout_s"),
                model=args.get("model"),
            )
        except core.BridgeError as exc:
            return _error(str(exc), exc.hint)
        except Exception as exc:  # pragma: no cover
            log(traceback.format_exc())
            return _error(f"{type(exc).__name__}: {exc}")

        if not res.get("ok"):
            attempts = res.get("attempts") or []
            tail = "; ".join(f"#{a.get('attempt')} {a.get('error')}" for a in attempts) or "no detail"
            return _error(f"Generation failed: {res.get('message')}. Attempts -> {tail}", res.get("hint"))

        lines = [f"files: {len(res['files'])}", f"size: {res['params']['width']}x{res['params']['height']}",
                 f"seed: {res['params']['seed']}", f"model: {res.get('model')}",
                 f"elapsed: {res['elapsed_s']}s"]
        for f in res["files"]:
            lines.append(f"path: {f['path']}")
        if res.get("notes"):
            lines.append("notes: " + " | ".join(res["notes"]))
        out = dict(res)
        out["summary"] = "\n".join(lines)
        return _text(out)

    if name == "locmodel_status":
        try:
            return _text(core.get_status(cfg))
        except core.BridgeError as exc:
            return _error(str(exc), exc.hint)

    if name == "list_models":
        try:
            base, port, src = core.base_url(cfg=cfg)
        except core.BridgeError as exc:
            return _error(str(exc), exc.hint)
        path = core.cfg_get(cfg, "engine.models_path", "/sdapi/v1/sd-models")
        code, obj = core.http_json(base + path, timeout=10)
        return _text({"ok": code == 200, "port": port, "detected_by": src, "models": obj})

    if name == "engine_doctor":
        try:
            res = core.doctor(cfg)
        except Exception as exc:  # pragma: no cover
            log(traceback.format_exc())
            return _error(f"{type(exc).__name__}: {exc}")
        if not res.get("ok"):
            return {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=2)}],
                    "isError": False}
        return _text(res)

    if name == "engine_discover":
        try:
            port, src = core.discover(cfg, force=True)
        except Exception as exc:  # pragma: no cover
            return _error(f"{type(exc).__name__}: {exc}")
        if not port:
            return _error("No engine found.", "Start LocModel and load a model, then retry.")
        return _text({"ok": True, "port": port, "detected_by": src})

    return _error(f"Unknown tool: {name}")


def handle(req: dict) -> dict | None:
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    if method == "initialize":
        requested = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        return ok({
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Local offline image generation. Call generate_image with an optimised English "
                "prompt; images are written to disk and absolute paths are returned. "
                "Call engine_doctor first if generation fails."
            ),
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return err(-32602, "arguments must be an object")
        try:
            return ok(call_tool(name, args))
        except Exception as exc:  # pragma: no cover
            log(traceback.format_exc())
            return ok(_error(f"Internal error: {type(exc).__name__}: {exc}"))
    if method in ("resources/list", "resources/templates/list", "prompts/list"):
        key = method.split("/")[0]
        return ok({key: []})
    if method and method.startswith("notifications/"):
        return None
    return err(-32601, f"Method not found: {method}")


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _read_messages():
    """Yield dicts. Accepts newline-delimited JSON and LSP Content-Length framing."""
    stream = sys.stdin.buffer
    while True:
        line = stream.readline()
        if not line:
            return
        text = line.decode("utf-8", "replace").strip()
        if not text:
            continue                      # blank separator between frames
        if text.lower().startswith("content-length:"):
            try:
                length = int(text.split(":", 1)[1].strip())
            except ValueError:
                log(f"bad Content-Length header ignored: {text!r}")
                continue
            # swallow any further headers up to and including the blank line
            while True:
                nxt = stream.readline()
                if not nxt or not nxt.strip():
                    break
            body = stream.read(length)
        else:
            body = text.encode("utf-8")
        if not body:
            continue
        try:
            msg = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            log(f"invalid JSON ignored: {body[:120]!r}")
            continue
        if isinstance(msg, list):
            for m in msg:
                if isinstance(m, dict):
                    yield m
        elif isinstance(msg, dict):
            yield msg


def main() -> int:
    log(f"started v{SERVER_VERSION}, python={sys.version.split()[0]}, pid={os.getpid()}")
    if _CORE_IMPORT_ERROR:
        log(f"WARNING core import failed: {_CORE_IMPORT_ERROR}")
    for msg in _read_messages():
        try:
            resp = handle(msg)
        except Exception as exc:  # pragma: no cover
            log(traceback.format_exc())
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": f"Internal error: {exc}"}}
        if resp is not None:
            _send(resp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
