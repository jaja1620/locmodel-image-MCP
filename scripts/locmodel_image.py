#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LocModel / stable-diffusion.cpp Image Bridge  (v2.0.0)

Cross-platform bridge that turns a local ``sd-server`` (stable-diffusion.cpp)
HTTP engine -- typically shipped inside LocModel -- into a scriptable CLI that
emits JSON, so that AI agents, MCP servers and shell scripts can generate
images fully offline.

Design goals of v2
------------------
* No hard-coded user paths, ports, resolutions or sampler defaults.
* Works on Windows / Linux / macOS (process discovery, GPU query, locks).
* Engine defaults are *read from the engine* (``/sdcpp/v1/capabilities``),
  never guessed, so non-turbo (SD1.5 / SDXL / Flux) models behave correctly.
* Adaptive size guard: guarded by total pixel area (not a single edge), with
  an optional, configurable upper bound derived from free VRAM.
* Failure taxonomy: parameter errors are not retried, capacity/VRAM errors are
  retried with a *down-scaled* request, transient errors are retried with a new
  seed.
* Everything is inspectable: ``doctor`` self-check, ``config`` command,
  structured ``--log-file``.

Endpoints used (stable-diffusion.cpp server)
-------------------------------------------
POST /sdcpp/v1/img_gen                -> {"id": "...", "poll_url": "..."}
GET  /sdcpp/v1/jobs/<id>              -> {"status": ..., "result": {...}}
POST /sdcpp/v1/jobs/<id>/cancel
GET  /sdcpp/v1/capabilities           -> defaults / limits
GET  /sdcpp/v1/models/status
GET  /sdapi/v1/sd-models              -> A1111-compatible model list

Usage
-----
python locmodel_image.py doctor
python locmodel_image.py status
python locmodel_image.py generate --prompt "a quiet courtyard" --width 1024 --height 1024
python locmodel_image.py generate --prompt "..." --aspect 16:9 --out ./output
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

VERSION = "2.0.0"
APP_NAME = "locmodel-image"

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - exotic streams
        pass

LOG = logging.getLogger(APP_NAME)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULTS: dict = {
    "port": None,
    "host": "127.0.0.1",
    "port_candidates": [
        8790, 8791, 8792, 8793, 8794, 8795, 8796, 8797, 8798, 8799,
        7860, 7861, 8080, 8188, 5000, 1234, 3000, 8000,
    ],
    "engine": {
        "process_names": ["sd-server", "sd-server.exe", "sd.exe", "stable-diffusion"],
        "capabilities_path": "/sdcpp/v1/capabilities",
        "img_gen_path": "/sdcpp/v1/img_gen",
        "jobs_path": "/sdcpp/v1/jobs",
        "models_path": "/sdapi/v1/sd-models",
        "status_path": "/sdcpp/v1/models/status",
        "probe_timeout": 1.5,
    },
    "size": {
        # Guard by TOTAL AREA, not by a single edge: some engines fail on
        # 512x512 (262k px) while 1280x720 (922k px) succeeds.
        "min_area": 768 * 768,
        "multiple": 8,
        "max_side": 1536,
        "max_side_auto": True,
        "hard_max_side": 2048,
    },
    "generate": {
        "timeout": 900,
        "retry": 2,
        "poll_interval": 2.0,
        "max_count": 4,
        "max_download_mb": 64,
        "lock": True,
        "lock_wait": 900,
    },
    "gpu": {"min_free_mb_warn": 500},
    "output": {"dir": None},
}

CONFIG_PATH = Path(
    os.environ.get("LOCMODEL_IMAGE_CONFIG")
    or (Path.home() / ".locmodel-image" / "config.json")
)

# The config file actually in use for this process (``--config`` may override it).
_ACTIVE_CONFIG_PATH: Path = CONFIG_PATH


def active_config_path() -> Path:
    return _ACTIVE_CONFIG_PATH


def set_config_path(path: str | Path | None) -> Path:
    """Point every read/write (including the generate lock) at one config file."""
    global _ACTIVE_CONFIG_PATH
    _ACTIVE_CONFIG_PATH = Path(path).expanduser().resolve() if path else CONFIG_PATH
    return _ACTIVE_CONFIG_PATH

_EXIT = {
    "ok": 0,
    "generic_error": 1,
    "generation_failed": 2,
    "engine_not_found": 3,
    "bad_arguments": 4,
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | None = None) -> dict:
    path = path or active_config_path()
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    if path.exists():
        try:
            cfg = _deep_merge(cfg, json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # pragma: no cover
            LOG.warning("config unreadable (%s): %s", path, exc)
    env_port = os.environ.get("LOCMODEL_SD_PORT")
    if env_port and env_port.isdigit():
        cfg["port"] = int(env_port)
    if os.environ.get("LOCMODEL_IMAGE_OUT"):
        cfg.setdefault("output", {})["dir"] = os.environ["LOCMODEL_IMAGE_OUT"]
    return cfg


def save_config(cfg: dict, path: Path | None = None) -> Path:
    path = path or active_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def cfg_get(cfg: dict, dotted: str, fallback=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return fallback
        cur = cur[part]
    return cur


def _coerce(text: str):
    try:
        return json.loads(text)
    except Exception:
        return text


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class BridgeError(RuntimeError):
    def __init__(self, message: str, code: str = "generic_error", hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


class EngineNotFound(BridgeError):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message, "engine_not_found", hint or (
            "Start LocModel, open the image-generation page and load a model, "
            "then retry. Alternatively set LOCMODEL_SD_PORT to the engine port."
        ))


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_json(url: str, payload=None, timeout: float = 60, method: str | None = None):
    """Return (status_code | None, obj_or_text). Never raises for HTTP errors."""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=data, headers=headers,
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(text)
            except Exception:
                return resp.status, text
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, body
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _is_loopback(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


# --------------------------------------------------------------------------
# Engine discovery (cross-platform)
# --------------------------------------------------------------------------

def probe(base: str, cfg: dict) -> bool:
    path = cfg_get(cfg, "engine.capabilities_path", "/sdcpp/v1/capabilities")
    timeout = cfg_get(cfg, "engine.probe_timeout", 1.5)
    code, obj = http_json(base + path, timeout=timeout)
    if code == 200 and isinstance(obj, dict):
        return ("current_mode" in obj) or ("defaults" in obj) or ("capabilities" in obj)
    return False


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        return (out.stdout or "") + (out.stderr or "")
    except Exception:
        return ""


def port_from_process(cfg: dict) -> list[int]:
    """Parse ``--listen-port`` / ``--port`` from running engine processes."""
    names = [n.lower() for n in cfg_get(cfg, "engine.process_names", [])]
    ports: list[int] = []
    texts: list[str] = []

    if os.name == "nt":
        for name in {n for n in names}:
            ps = (
                "Get-CimInstance Win32_Process -Filter \"Name='{0}'\" | "
                "Select-Object -ExpandProperty CommandLine"
            ).format(name)
            out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
            texts.extend(out.splitlines())
    else:
        # macOS / Linux: single ps pass, filter by process name
        out = _run(["ps", "-axo", "command="])
        for line in out.splitlines():
            low = line.lower()
            if any(n in low for n in names):
                texts.append(line)
        # Linux: /proc/*/cmdline is more reliable when ps output is truncated
        proc = Path("/proc")
        if proc.is_dir():
            for entry in proc.glob("[0-9]*/cmdline"):
                try:
                    raw = entry.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
                except Exception:
                    continue
                low = raw.lower()
                if any(n in low for n in names):
                    texts.append(raw)

    for line in texts:
        for flag in ("--listen-port", "-p", "--port"):
            for m in re.finditer(rf"{re.escape(flag)}[\s=]+(\d{{2,5}})", line):
                p = int(m.group(1))
                if 1 <= p <= 65535 and p not in ports:
                    ports.append(p)
    return ports


def _local_listen_ports() -> list[int]:
    """Cheap enumeration of locally listening TCP ports (best effort)."""
    ports: set[int] = set()
    if os.name == "nt":
        out = _run(["netstat", "-ano", "-p", "TCP"], timeout=15)
        for line in out.splitlines():
            m = re.search(r"127\.0\.0\.1:(\d+)\s+\S+\s+LISTENING", line)
            if m:
                ports.add(int(m.group(1)))
    else:
        out = _run(["sh", "-c", "netstat -ltn 2>/dev/null || ss -ltn 2>/dev/null"], timeout=15)
        for line in out.splitlines():
            m = re.search(r"(?:127\.0\.0\.1|\[::1\]|0\.0\.0\.0|\*):(\d+)\s", line)
            if m:
                ports.add(int(m.group(1)))
    return sorted(ports)


def discover(cfg: dict, force: bool = False) -> tuple[int | None, str]:
    """Priority: env > cached config port (verified) > process cmdline > candidates > listen scan."""
    host = cfg_get(cfg, "host", "127.0.0.1")

    env = os.environ.get("LOCMODEL_SD_PORT")
    if env and env.strip():
        # An explicitly pinned port is authoritative: if nothing answers there we
        # say so instead of silently drifting to some other engine on the box.
        if not env.strip().isdigit():
            raise EngineNotFound(f"LOCMODEL_SD_PORT={env!r} is not a valid port number.")
        if probe(f"http://{host}:{env.strip()}", cfg):
            return int(env.strip()), "env"
        raise EngineNotFound(
            f"LOCMODEL_SD_PORT={env.strip()} is pinned but nothing answers there.",
            hint=(f"Start LocModel on port {env.strip()}, or unset LOCMODEL_SD_PORT "
                  "to let auto-discovery find the engine."),
        )

    cached = cfg.get("port")
    if cached and not force and probe(f"http://{host}:{cached}", cfg):
        return int(cached), "config"

    seen: set[int] = set()
    for p in port_from_process(cfg):
        seen.add(p)
        if probe(f"http://{host}:{p}", cfg):
            _remember_port(cfg, p)
            return p, "process"

    candidates = [p for p in cfg_get(cfg, "port_candidates", []) if p not in seen]
    for p in candidates:
        if probe(f"http://{host}:{p}", cfg):
            _remember_port(cfg, p)
            return p, "candidate"

    for p in _local_listen_ports():
        if p in seen or p in candidates:
            continue
        if probe(f"http://{host}:{p}", cfg):
            _remember_port(cfg, p)
            return p, "listen_scan"

    return None, "not_found"


def _remember_port(cfg: dict, port: int) -> None:
    try:
        cfg["port"] = port
        cfg["updated"] = datetime.now().isoformat(timespec="seconds")
        save_config(cfg)
    except Exception as exc:  # pragma: no cover
        LOG.debug("cannot persist port: %s", exc)


def base_url(force: bool = False, cfg: dict | None = None) -> tuple[str, int, str]:
    cfg = cfg or load_config()
    port, src = discover(cfg, force=force)
    if not port:
        raise EngineNotFound(
            "No stable-diffusion.cpp engine found on this machine."
        )
    host = cfg_get(cfg, "host", "127.0.0.1")
    return f"http://{host}:{port}", port, src


# --------------------------------------------------------------------------
# Engine introspection
# --------------------------------------------------------------------------

def capabilities(base: str, cfg: dict) -> dict:
    path = cfg_get(cfg, "engine.capabilities_path", "/sdcpp/v1/capabilities")
    code, obj = http_json(base + path, timeout=10)
    return obj if isinstance(obj, dict) else {}


def model_status(base: str, cfg: dict) -> dict:
    path = cfg_get(cfg, "engine.status_path", "/sdcpp/v1/models/status")
    code, obj = http_json(base + path, timeout=10)
    return obj if isinstance(obj, dict) else {}


def current_model(base: str, cfg: dict) -> str | None:
    path = cfg_get(cfg, "engine.models_path", "/sdapi/v1/sd-models")
    code, obj = http_json(base + path, timeout=10)
    if isinstance(obj, list) and obj:
        first = obj[0]
        if isinstance(first, dict):
            return first.get("model_name") or first.get("title")
    st = model_status(base, cfg)
    for key in ("model", "model_name", "diffusion_model"):
        if isinstance(st.get(key), str):
            return st[key]
    return None


def engine_defaults(base: str, cfg: dict) -> dict:
    """Read generation defaults from the engine instead of hard-coding them."""
    caps = capabilities(base, cfg)
    d = (caps.get("defaults") or {}) if isinstance(caps, dict) else {}
    sp = d.get("sample_params") or {}
    guidance = sp.get("guidance") or {}
    return {
        "width": d.get("width"),
        "height": d.get("height"),
        "steps": sp.get("sample_steps"),
        "sampler": sp.get("sample_method"),
        "cfg": guidance.get("txt_cfg"),
        "batch_count": d.get("batch_count"),
    }


def gpu_memory() -> dict | None:
    """Multi-vendor best-effort VRAM query. Returns None when unknown."""
    # NVIDIA
    if shutil.which("nvidia-smi"):
        out = _run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            timeout=10,
        ).strip()
        if out:
            try:
                name, total, used, free = [x.strip() for x in out.splitlines()[0].split(",")]
                return {"vendor": "nvidia", "name": name, "total_mb": int(total),
                        "used_mb": int(used), "free_mb": int(free)}
            except Exception:
                pass
    # AMD ROCm
    if shutil.which("rocm-smi"):
        out = _run(["rocm-smi", "--showmeminfo", "vram"], timeout=10)
        tot = re.search(r"VRAM Total Memory \(B\):\s*(\d+)", out)
        used = re.search(r"VRAM Total Used Memory \(B\):\s*(\d+)", out)
        if tot and used:
            t = int(tot.group(1)) // (1024 * 1024)
            u = int(used.group(1)) // (1024 * 1024)
            return {"vendor": "amd", "name": "AMD GPU", "total_mb": t,
                    "used_mb": u, "free_mb": max(0, t - u)}
    return None


def get_status(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    base, port, src = base_url(cfg=cfg)
    caps = capabilities(base, cfg)
    st = model_status(base, cfg)
    d = engine_defaults(base, cfg)
    return {
        "ok": True,
        "version": VERSION,
        "engine": {"base": base, "port": port, "detected_by": src},
        "model": current_model(base, cfg),
        "model_status": {"origin": st.get("origin"), "recipe": st.get("recipe")},
        "defaults": d,
        "gpu": gpu_memory(),
        "size_policy": _size_policy(cfg, None),
        "config_path": str(active_config_path()),
    }


# --------------------------------------------------------------------------
# Size policy
# --------------------------------------------------------------------------

def _size_policy(cfg: dict, gpu: dict | None) -> dict:
    smin = int(cfg_get(cfg, "size.min_area", 768 * 768))
    mult = int(cfg_get(cfg, "size.multiple", 8))
    hard_max = int(cfg_get(cfg, "size.hard_max_side", 2048))
    max_side = int(cfg_get(cfg, "size.max_side", 1536))
    auto = bool(cfg_get(cfg, "size.max_side_auto", True))
    if auto and gpu and gpu.get("total_mb"):
        total = gpu["total_mb"]
        if total <= 4096:
            max_side = min(max_side, 1024)
        elif total <= 6144:
            max_side = min(max_side, 1280)
        else:
            max_side = min(max_side, 1536)
    max_side = max(mult, min(max_side, hard_max))
    return {"min_area": smin, "min_side_hint": int(round(smin ** 0.5 / mult) * mult),
            "max_side": max_side, "multiple": mult, "auto_from_vram": auto}


def _align(v: float, mult: int) -> int:
    return max(mult, int(round(v / mult) * mult))


def normalize_size(width, height, cfg: dict, gpu: dict | None = None,
                   guard: bool = True, downscale: float = 1.0) -> tuple[int, int, list[str]]:
    """Align to multiple, enforce minimum pixel area and maximum edge.

    Guarding by *area* preserves the user's aspect ratio intent instead of
    forcing a square minimum edge.
    """
    pol = _size_policy(cfg, gpu)
    mult, min_area, max_side = pol["multiple"], pol["min_area"], pol["max_side"]
    notes: list[str] = []
    try:
        w, h = int(width), int(height)
    except Exception:
        raise BridgeError(f"width/height must be integers, got {width!r}/{height!r}", "bad_arguments")
    if w <= 0 or h <= 0:
        raise BridgeError("width/height must be positive", "bad_arguments")

    if downscale and downscale != 1.0:
        w, h = w * downscale, h * downscale

    if guard:
        if w * h < min_area:
            scale = (min_area / float(w * h)) ** 0.5
            nw, nh = _align(w * scale, mult), _align(h * scale, mult)
            notes.append(f"size {int(w)}x{int(h)} is below the engine's stable area; upscaled to {nw}x{nh}")
            w, h = nw, nh
        if max(w, h) > max_side:
            scale = max_side / float(max(w, h))
            nw, nh = _align(w * scale, mult), _align(h * scale, mult)
            notes.append(f"size {int(w)}x{int(h)} exceeds the safe long edge {max_side}px; reduced to {nw}x{nh}")
            w, h = nw, nh

    aw, ah = _align(w, mult), _align(h, mult)
    if (aw, ah) != (int(w), int(h)):
        notes.append(f"size aligned to a multiple of {mult}: {aw}x{ah}")
    return aw, ah, notes


ASPECTS = {
    "1:1": (1, 1), "4:3": (4, 3), "3:4": (3, 4), "3:2": (3, 2), "2:3": (2, 3),
    "16:9": (16, 9), "9:16": (9, 16), "21:9": (21, 9), "2:1": (2, 1), "1:2": (1, 2),
}


def aspect_to_size(aspect: str, area: int | None = None) -> tuple[int, int]:
    if aspect not in ASPECTS:
        raise BridgeError(f"unknown aspect {aspect!r}; supported: {', '.join(ASPECTS)}", "bad_arguments")
    a, b = ASPECTS[aspect]
    area = int(area or (1024 * 1024))
    ratio = a / b
    h = (area / ratio) ** 0.5
    w = h * ratio
    return int(round(w)), int(round(h))


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

def _lock_path() -> Path:
    return active_config_path().parent / "generate.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":  # tasklist avoids killing anything by accident
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=15).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return True  # unknown -> assume alive, never steal a live lock


def _acquire_lock(cfg: dict, wait: int | None = None):
    """Serialise generations across processes.

    Two concurrent jobs fighting over one GPU is the most common cause of
    ``workspace capacity`` failures, so callers queue by default.  A lock whose
    owner died is reclaimed immediately instead of stalling the caller.
    """
    if not bool(cfg_get(cfg, "generate.lock", True)):
        return None
    wait = int(wait if wait is not None else cfg_get(cfg, "generate.lock_wait", 900))
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + wait
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}".encode())
            os.close(fd)
            return path
        except FileExistsError:
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                owner = int(text.split()[0]) if text else 0
            except FileNotFoundError:
                continue
            except Exception:
                owner = 0
            if owner and not _pid_alive(owner):
                LOG.warning("reclaiming stale lock left by dead pid %s", owner)
            elif (time.time() - path.stat().st_mtime) > wait + 600:
                LOG.warning("reclaiming expired lock")
            elif time.time() > deadline:
                LOG.warning("lock wait timed out after %ss; proceeding without lock", wait)
                return None
            else:
                LOG.info("another generation is running (pid %s), waiting ...", owner or "?")
                time.sleep(5)
                continue
            try:
                path.unlink()
            except Exception:
                pass
        except Exception as exc:  # pragma: no cover
            LOG.debug("lock unavailable (%s); proceeding unlocked", exc)
            return None


def _release_lock(path: Path | None) -> None:
    if not path:
        return
    try:
        path.unlink()
    except Exception:  # pragma: no cover
        pass


def _classify_failure(msg: str) -> str:
    low = (msg or "").lower()
    if any(k in low for k in ("workspace", "capacity", "out of memory", "oom",
                              "no results", "generation_failed", "alloc")):
        return "capacity"
    if any(k in low for k in ("invalid", "unsupported", "bad request", "not found",
                              "model not loaded", "unknown")):
        return "parameter"
    return "transient"


def _decode_image(img: dict, max_bytes: int) -> bytes | None:
    b64 = img.get("b64_json") or img.get("b64") or ""
    if b64:
        if b64.startswith("data:"):
            b64 = b64.split(",", 1)[-1]
        try:
            data = base64.b64decode(b64, validate=False)
        except Exception:
            return None
        return data if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:2] == b"\xff\xd8" else data
    url = img.get("url")
    if url:
        if not _is_loopback(url):
            LOG.warning("refusing to download image from non-loopback url: %s", url)
            return None
        code, raw = http_json(url, timeout=120)
        if isinstance(raw, str):
            return None
        return raw if isinstance(raw, (bytes, bytearray)) and len(raw) < max_bytes else None
    return None


def generate(prompt: str = "", cfg: dict | None = None, *, negative_prompt: str = "",
             width: int | None = None, height: int | None = None,
             aspect: str | None = None, steps: int | None = None,
             sampler: str | None = None, cfg_scale: float | None = None,
             seed: int = -1, count: int = 1, out_dir: str | None = None,
             filename_prefix: str = "locmodel", timeout: int | None = None,
             retry: int | None = None, guard: bool = True, model: str | None = None,
             dry_run: bool = False) -> dict:
    cfg = cfg or load_config()
    if not prompt or not prompt.strip():
        raise BridgeError("prompt must not be empty", "bad_arguments")

    # Validate cheap arguments BEFORE touching the engine so that misuse never
    # costs a network round-trip.
    max_count = int(cfg_get(cfg, "generate.max_count", 4))
    if int(count) > max_count:
        raise BridgeError(f"count={count} exceeds generate.max_count={max_count}", "bad_arguments")
    for name, val in (("width", width), ("height", height)):
        if val is not None and int(val) <= 0:
            raise BridgeError(f"{name} must be a positive integer, got {val}", "bad_arguments")
    if aspect is not None and aspect not in ASPECTS:
        raise BridgeError(f"unknown aspect {aspect!r}; supported: {', '.join(ASPECTS)}", "bad_arguments")

    base, port, src = base_url(cfg=cfg)
    gpu = gpu_memory()
    eng = engine_defaults(base, cfg)

    if aspect and not (width and height):
        w0, h0 = aspect_to_size(aspect, (width or height or 1024) ** 2 or 1024 * 1024)
        width, height = w0, h0
    width = int(width or eng.get("width") or 1024)
    height = int(height or eng.get("height") or 1024)
    steps = int(steps if steps is not None else (eng.get("steps") or 20))
    sampler = sampler or eng.get("sampler") or "euler"
    if cfg_scale is None:
        cfg_scale = eng.get("cfg")

    if seed is None or int(seed) < 0:
        seed = random.randint(1, 2 ** 31 - 1)
    seed = int(seed)
    timeout = int(timeout or cfg_get(cfg, "generate.timeout", 900))
    retry = int(retry if retry is not None else cfg_get(cfg, "generate.retry", 2))
    poll = float(cfg_get(cfg, "generate.poll_interval", 2.0))

    w, h, notes = normalize_size(width, height, cfg, gpu, guard=guard)
    if gpu and gpu.get("free_mb") is not None and gpu["free_mb"] < int(cfg_get(cfg, "gpu.min_free_mb_warn", 500)):
        notes.append(
            f"free VRAM is low ({gpu['free_mb']} MB). A resident model normally keeps it low, "
            "so this is informational; if generation fails, close other GPU apps "
            "(LM Studio, browser hardware acceleration) or unload unused models."
        )

    payload: dict = {
        "prompt": prompt.strip(),
        "negative_prompt": (negative_prompt or "").strip(),
        "width": w,
        "height": h,
        "batch_count": max(1, int(count)),
        "seed": seed,
        "sample_params": {"sample_steps": max(1, steps), "sample_method": sampler},
    }
    if cfg_scale is not None:
        payload["sample_params"]["guidance"] = {"txt_cfg": float(cfg_scale)}
    if model:
        payload["model"] = model

    out_path = Path(out_dir or cfg_get(cfg, "output.dir") or (Path.cwd() / "output"))

    if dry_run:
        return {"ok": True, "dry_run": True, "payload": payload, "size_notes": notes,
                "engine": {"base": base, "port": port, "detected_by": src},
                "out_dir": str(out_path), "engine_defaults": eng, "size_policy": _size_policy(cfg, gpu)}

    out_path.mkdir(parents=True, exist_ok=True)
    job_path = cfg_get(cfg, "engine.img_gen_path", "/sdcpp/v1/img_gen")
    jobs_path = cfg_get(cfg, "engine.jobs_path", "/sdcpp/v1/jobs")

    lock = _acquire_lock(cfg)
    attempts: list[dict] = []
    images: list[dict] = []
    job_id = None
    t0 = time.time()
    current_scale = 1.0
    max_download = int(cfg_get(cfg, "generate.max_download_mb", 64)) * 1024 * 1024

    for attempt in range(1, max(1, retry) + 1):
        code, obj = http_json(base + job_path, payload=payload, timeout=60)
        if code not in (200, 201, 202) or not isinstance(obj, dict):
            body = str(obj)[:400]
            kind = "transient" if (code is None or code >= 500) else "parameter"
            attempts.append({"attempt": attempt, "stage": "submit", "http": code,
                             "kind": kind, "error": body})
            if kind == "parameter":
                break
            time.sleep(min(2 * attempt, 8))
            continue

        job_id = obj.get("id")
        deadline = time.time() + timeout
        state: dict | None = None
        polls = 0
        last_poll_err: str | None = None
        while time.time() < deadline:
            time.sleep(poll)
            polls += 1
            try:
                sc, st = http_json(f"{base}{jobs_path}/{job_id}", timeout=30)
            except Exception as exc:  # noqa: BLE001 - a slow poll must never abandon a running job
                last_poll_err = f"{type(exc).__name__}: {exc}"
                if polls % 10 == 0:
                    LOG.info("poll #%d for %s failed (%s), still waiting", polls, job_id, last_poll_err)
                continue
            if isinstance(st, dict):
                state = st
                if st.get("status") in ("succeeded", "completed", "failed", "cancelled"):
                    break

        status = (state or {}).get("status")
        if status is None and job_id:
            # We stopped waiting; cancel so the engine is not left crunching a job nobody will read.
            try:
                cc, _ = http_json(f"{base}{jobs_path}/{job_id}/cancel", payload={}, timeout=15)
                attempts.append({"attempt": attempt, "stage": "cancel", "http": cc})
                LOG.info("cancelled job %s after giving up (http %s)", job_id, cc)
            except Exception as exc:  # noqa: BLE001 - cancellation is best-effort
                LOG.info("cancel request for %s failed: %s", job_id, exc)
        if status in ("succeeded", "completed"):
            images = ((state.get("result") or {}).get("images") or [])
            attempts.append({"attempt": attempt, "stage": "poll", "status": status, "images": len(images)})
            break

        err = ((state or {}).get("error") or {})
        msg = (err.get("message") or err.get("code")
               or (f"timeout after {timeout}s ({last_poll_err})" if status is None and last_poll_err
                   else "timeout" if status is None else str(status)))
        kind = _classify_failure(f"{err.get('code', '')} {msg}")
        attempts.append({"attempt": attempt, "stage": "poll", "status": status,
                         "kind": kind, "error": err or msg})

        if attempt >= retry or kind == "parameter":
            break
        if kind == "capacity":
            current_scale *= 0.85
            nw, nh, extra = normalize_size(int(payload["width"] * current_scale),
                                           int(payload["height"] * current_scale),
                                           cfg, gpu, guard=guard)
            if (nw, nh) == (payload["width"], payload["height"]):
                payload["seed"] = random.randint(1, 2 ** 31 - 1)
            else:
                payload["width"], payload["height"] = nw, nh
                notes.extend(extra)
        else:
            payload["seed"] = random.randint(1, 2 ** 31 - 1)
        time.sleep(2)

    _release_lock(lock)
    elapsed = round(time.time() - t0, 1)

    if not images:
        return {
            "ok": False, "error": "generation_failed", "code": "generation_failed",
            "message": "Engine produced no image. Typical causes: insufficient VRAM, "
                       "unsupported size for the loaded model, or the model is still loading.",
            "attempts": attempts,
            "params": {k: v for k, v in payload.items() if k != "sample_params"},
            "notes": notes, "elapsed_s": elapsed, "job_id": job_id,
            "hint": "Run `doctor`, retry with a smaller size (e.g. 1024x1024) or fewer steps, "
                    "or verify manually inside the LocModel UI.",
        }

    files = []
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = re.sub(r"[^\w\-]+", "_", filename_prefix or "locmodel")[:40] or "locmodel"
    for i, img in enumerate(images):
        data = _decode_image(img, max_download)
        if not data:
            continue
        ext = ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
        fp = out_path / f"{prefix}_{stamp}_{i + 1}_seed{seed}{ext}"
        n = 2
        while fp.exists():
            fp = out_path / f"{prefix}_{stamp}_{i + 1}_seed{seed}_{n}{ext}"
            n += 1
        fp.write_bytes(data)
        files.append({"path": str(fp.resolve()), "bytes": len(data)})

    meta = {
        "app": APP_NAME, "version": VERSION, "prompt": payload["prompt"],
        "negative_prompt": payload["negative_prompt"], "width": payload["width"],
        "height": payload["height"], "steps": steps, "sampler": sampler,
        "cfg": cfg_scale, "seed": seed, "count": count,
        "model": current_model(base, cfg), "job_id": job_id, "port": port,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        (out_path / f"{prefix}_{stamp}_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # pragma: no cover
        LOG.debug("cannot write meta: %s", exc)

    return {
        "ok": True, "files": files,
        "params": {"prompt": payload["prompt"], "negative_prompt": payload["negative_prompt"],
                   "width": payload["width"], "height": payload["height"],
                   "steps": steps, "sampler": sampler, "cfg": cfg_scale,
                   "seed": seed, "count": count},
        "model": meta["model"], "attempts": attempts, "notes": notes,
        "elapsed_s": elapsed, "job_id": job_id,
        "engine": {"base": base, "port": port, "detected_by": src},
    }


# --------------------------------------------------------------------------
# Doctor
# --------------------------------------------------------------------------

def doctor(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    checks: list[dict] = []

    def add(name: str, ok, detail: str, level: str = "info"):
        checks.append({"name": name, "ok": bool(ok), "level": level, "detail": detail})

    add("python", sys.version_info >= (3, 8),
        f"{sys.version.split()[0]} at {sys.executable}", "error" if sys.version_info < (3, 8) else "info")
    add("platform", True, f"{os.name} / {sys.platform}")
    add("config", True, f"{active_config_path()} ({'exists' if active_config_path().exists() else 'defaults'})")

    port, src = discover(cfg)
    add("engine", bool(port), f"port={port} via {src}" if port else "no engine found", "error" if not port else "info")

    if port:
        base = f"http://{cfg_get(cfg, 'host', '127.0.0.1')}:{port}"
        caps = capabilities(base, cfg)
        add("capabilities", bool(caps), f"{len(caps)} keys" if caps else "empty response", "warn" if not caps else "info")
        cm = current_model(base, cfg)
        add("model", bool(cm), cm or "no model reported", "warn" if not cm else "info")
        eng = engine_defaults(base, cfg)
        add("engine_defaults", bool(eng.get("width")), json.dumps(eng, ensure_ascii=False))

    gpu = gpu_memory()
    add("gpu", gpu is not None, json.dumps(gpu, ensure_ascii=False) if gpu else "not detected (CPU / unknown vendor)", "warn")
    pol = _size_policy(cfg, gpu)
    add("size_policy", True, json.dumps(pol, ensure_ascii=False))

    out = Path(cfg_get(cfg, "output.dir") or (Path.cwd() / "output"))
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe_file = out / ".write_test"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        add("output_writable", True, str(out))
    except Exception as exc:
        add("output_writable", False, f"{out}: {exc}", "error")

    add("tools", True, " ".join(f"{t}={'yes' if shutil.which(t) else 'no'}"
                                for t in ("nvidia-smi", "rocm-smi", "netstat", "ps", "powershell")))
    ok = all(c["ok"] for c in checks if c["level"] == "error")
    return {"ok": ok, "version": VERSION, "checks": checks}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="locmodel_image.py",
        description=f"LocModel / stable-diffusion.cpp image bridge v{VERSION}",
    )
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    ap.add_argument("--config", help="path to config.json (default: ~/.locmodel-image/config.json)")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--log-file", help="append debug logs to this file")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="engine status, model, engine defaults, GPU")
    sub.add_parser("discover", help="re-discover the engine port")
    sub.add_parser("models", help="list models reported by the engine")
    sub.add_parser("doctor", help="self-check environment, engine and output path")

    g = sub.add_parser("generate", help="submit a generation job and wait for the result")
    g.add_argument("--prompt", required=True)
    g.add_argument("--negative", default="")
    g.add_argument("--width", type=int)
    g.add_argument("--height", type=int)
    g.add_argument("--aspect", help=f"aspect preset: {', '.join(ASPECTS)}")
    g.add_argument("--steps", type=int)
    g.add_argument("--sampler")
    g.add_argument("--cfg", type=float, dest="cfg_scale")
    g.add_argument("--seed", type=int, default=-1)
    g.add_argument("--count", type=int, default=1)
    g.add_argument("--model", help="model name override (multi-model engines)")
    g.add_argument("--out", dest="out_dir")
    g.add_argument("--prefix", default="locmodel")
    g.add_argument("--timeout", type=int)
    g.add_argument("--retry", type=int)
    g.add_argument("--no-guard", action="store_true", help="disable min-area / max-edge guard")
    g.add_argument("--dry-run", action="store_true", help="print the payload without generating")

    c = sub.add_parser("config", help="show or modify configuration")
    c.add_argument("action", choices=["show", "get", "set", "path"])
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")

    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if args.log_file:
        fh = logging.FileHandler(args.log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(fh)

    cfg = load_config(set_config_path(args.config))

    try:
        if args.cmd == "status":
            _emit(get_status(cfg))
        elif args.cmd == "doctor":
            res = doctor(cfg)
            _emit(res)
            return 0 if res["ok"] else _EXIT["engine_not_found"]
        elif args.cmd == "discover":
            port, src = discover(cfg, force=True)
            _emit({"ok": bool(port), "port": port, "detected_by": src})
            return 0 if port else _EXIT["engine_not_found"]
        elif args.cmd == "models":
            base, port, _ = base_url(cfg=cfg)
            path = cfg_get(cfg, "engine.models_path", "/sdapi/v1/sd-models")
            code, obj = http_json(base + path, timeout=10)
            _emit({"ok": code == 200, "port": port, "models": obj})
        elif args.cmd == "generate":
            res = generate(
                args.prompt, cfg,
                negative_prompt=args.negative, width=args.width, height=args.height,
                aspect=args.aspect, steps=args.steps, sampler=args.sampler,
                cfg_scale=args.cfg_scale, seed=args.seed, count=args.count,
                out_dir=args.out_dir, filename_prefix=args.prefix,
                timeout=args.timeout, retry=args.retry, guard=not args.no_guard,
                model=args.model, dry_run=args.dry_run,
            )
            _emit(res)
            return 0 if res.get("ok") else _EXIT["generation_failed"]
        elif args.cmd == "config":
            if args.action == "path":
                _emit({"path": str(active_config_path()), "exists": active_config_path().exists()})
            elif args.action == "show":
                _emit(cfg)
            elif args.action == "get":
                if not args.key:
                    raise BridgeError("config get requires a key", "bad_arguments")
                _emit({args.key: cfg_get(cfg, args.key)})
            elif args.action == "set":
                if not args.key or args.value is None:
                    raise BridgeError("config set requires key and value", "bad_arguments")
                cur = cfg
                parts = args.key.split(".")
                for p in parts[:-1]:
                    cur = cur.setdefault(p, {})
                cur[parts[-1]] = _coerce(args.value)
                _emit({"ok": True, "saved": str(save_config(cfg)), "set": {args.key: cur[parts[-1]]}})
    except BridgeError as exc:
        _emit({"ok": False, "error": exc.code, "message": str(exc), "hint": exc.hint})
        return _EXIT.get(exc.code, 1)
    except Exception as exc:  # pragma: no cover
        _emit({"ok": False, "error": type(exc).__name__, "message": str(exc)})
        return _EXIT["generic_error"]
    return 0


if __name__ == "__main__":
    sys.exit(main())
