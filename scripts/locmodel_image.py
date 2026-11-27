#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LocModel / stable-diffusion.cpp Image Bridge  (v3.0.0)

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
import threading
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import locmodel_workflow as workflow

VERSION = "4.1.0"
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
        #: recipe list; only 3.6.x answers it (older engines return 404)
        "recipes_path": "/sdcpp/v1/recipes",
        "status_path": "/sdcpp/v1/models/status",
        "probe_timeout": 1.5,
    },
    "size": {
        # Guard by TOTAL AREA, not by a single edge: some engines fail on
        # 512x512 (262k px) while 1280x720 (922k px) succeeds.
        #
        # v3.2 changed the contract. These numbers only shape a size the tool
        # picks itself (aspect presets, or nothing specified). When the caller
        # passes width/height explicitly, the tool no longer rewrites them: it
        # only enforces what the engine actually rejects (limits.max_*), and
        # downgrades everything else (small area, big canvas vs free VRAM) to a
        # warning. v3.1 silently turned a requested 512x512 into 768x768 and
        # clamped 2048 to 1536, which is not the tool's call to make.
        "min_area": 768 * 768,
        "multiple": 8,
        #: preferred long edge when the tool picks the size itself
        "max_side": 1536,
        "max_side_auto": True,
        #: ceiling for the automatic policy, never above the engine's own limit
        "hard_max_side": 4096,
        #: honour an explicit width/height (only engine limits are enforced)
        "explicit_respect": True,
        #: warn when an explicit canvas needs more VRAM than this per megapixel
        #: (rough working-set estimate for a 4B flow model at 1024px)
        "vram_mb_per_mp": 1600,
    },
    # ---- v4.1 reference-image channel (LocModel 3.6.x) ---------------------
    # Measured on 3.6.5 / flux-2-klein-4b inside this machine's LocModel: the
    # engine takes a *plural* ref_images array, binds it at step 0, ignores
    # strength, negprompt and any extra payload key, and rounds the canvas to
    # the recipe's size_multiple. These knobs only shape what we send.
    "ref": {
        #: pixel budget for a canvas derived from the first reference
        "max_pixels": 1024 * 1024,
        #: hard ceiling; 1.57 MP was measured safe, well above it risks OOM
        "max_pixels_ceiling": 4 * 1024 * 1024,
        #: follow_first = derive the canvas from reference #1, image = keep ours
        "fit": "follow_first",
        "max_images": 8,
        #: per-file upload budget (base64 makes the payload ~1.37x bigger)
        "max_mb": 8,
        #: steps above this buy nothing on the ref channel (the ref is baked at t0)
        "steps_warn": 12,
    },
    "generate": {
        "timeout": 900,
        "retry": 2,
        "poll_interval": 2.0,
        "max_count": 8,
        "max_download_mb": 64,
        "lock": True,
        "lock_wait": 900,
    },
    "gpu": {"min_free_mb_warn": 500},
    "output": {"dir": None},
    # ---- v3.0 image editing ------------------------------------------------
    # Discovery is read-only probing, but it is not free: launching PowerShell to
    # read process command lines and connecting to closed ports both cost real
    # seconds on a managed machine. These knobs bound that work.
    "discover": {
        "process_cache_ttl": 20,
        "budget_seconds": 20,
        "listen_budget_seconds": 4,
    },
    "edit": {
        "engine_cache_ttl": 20,
        # How long a saved model snapshot stays valid across processes. CLI calls
        # are separate processes, so this is what keeps discovery off the hot path.
        "snapshot_cache_ttl": 120,
        # Which families may edit, best first. "qwen-image" is the weakest one:
        # it is an img2img reference model, it cannot honour a mask.
        "priority": ["qwen-image-edit", "flux2-klein", "qwen-image"],
        # Never switch models just to climb the priority list: an already loaded
        # model wins, because a switch costs 30-60 s (4B) or minutes (20B) and
        # may run out of VRAM.
        "prefer_loaded": True,
        # Load the best model automatically when none of the editable families
        # is loaded -- otherwise the priority list would be decorative.
        "auto_load": True,
        "auto_load_timeout": 240,
        # auto = try /sdcpp/v1/img_gen, fall back to /sdapi/v1/img2img.
        "channel": "auto",
        "strength": 0.75,
        "redraw_strength": 0.6,
        "timeout": 900,
        "mask_blur": 8,
        "inpaint_padding": 32,
        "verify": "agent",
        # Where to look for model files (in addition to what the engine reports).
        "models_dirs": [],
        "load_endpoint": None,
        "resolved_channel": None,
    },
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


_PROC_CACHE = {"stamp": 0.0, "key": "", "text": ""}

def _engine_process_exists(names: list[str]) -> bool:
    """Cheap pre-check: is any engine process running at all?

    Reading command lines costs one PowerShell start (~8 s on a managed machine),
    while listing process names costs a fraction of a second. When no engine is
    running there is nothing to parse, so skip the expensive half entirely.
    """
    try:
        out = _run(["tasklist", "/NH", "/FO", "CSV"], timeout=8)
    except Exception:
        return True
    low = (out or "").lower()
    if not low.strip():
        return True  # cannot tell -> let the precise path decide
    return any(n in low for n in names)


def _process_cmdlines(cfg: dict, force: bool = False) -> str:
    """Command lines of processes whose name looks like the engine.

    On Windows this is *one* PowerShell call for all configured names: launching
    PowerShell once per name costs several seconds each on managed machines,
    which used to make a cold discovery take tens of seconds for nothing.
    """
    try:
        ttl = float(cfg_get(cfg, "discover.process_cache_ttl", 20))
    except (TypeError, ValueError):
        ttl = 20.0
    names = [str(n).lower() for n in cfg_get(cfg, "engine.process_names", [])]
    key = "|".join(names)
    now = time.monotonic()
    if (not force and _PROC_CACHE["key"] == key and _PROC_CACHE["stamp"]
            and now - _PROC_CACHE["stamp"] < ttl):
        return _PROC_CACHE["text"]
    if os.name == "nt":
        if not _engine_process_exists(names):
            _PROC_CACHE.update(stamp=now, key=key, text="")
            return ""
        quoted = ",".join("'{0}'".format(n.replace("'", "''")) for n in names)
        script = ("$n=@({0}); Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
                  "Where-Object {{ $n -contains $_.Name.ToLower() }} | "
                  "Select-Object -ExpandProperty CommandLine").format(quoted)
        text = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=8)
    else:
        text = _run(["ps", "-axo", "command="], timeout=12)
        proc = Path("/proc")
        if proc.is_dir():
            extra = []
            for entry in proc.glob("[0-9]*/cmdline"):
                try:
                    raw = entry.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
                except Exception:
                    continue
                extra.append(raw)
            text = "\n".join([text] + extra)
    _PROC_CACHE.update(stamp=now, key=key, text=text or "")
    return _PROC_CACHE["text"]


def port_from_process(cfg: dict) -> list[int]:
    names = [str(n).lower() for n in cfg_get(cfg, "engine.process_names", [])]
    ports: list[int] = []
    texts: list[str] = []
    for line in _process_cmdlines(cfg).splitlines():
        if not line.strip():
            continue
        if os.name != "nt" and not any(n in line.lower() for n in names):
            continue
        texts.append(line)
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


def _probe_first(cfg: dict, ports: list[int], budget: float) -> int | None:
    """Probe ports concurrently; return the first responder in the given order.

    A connect that is dropped rather than refused costs the full probe timeout on
    hardened machines, so probing 20 candidates serially took half a minute.
    Eight at a time keeps the semantics (order decides the winner) and cuts the
    wall clock.
    """
    ports = [int(p) for p in ports]
    if not ports:
        return None
    host = cfg_get(cfg, "host", "127.0.0.1")
    deadline = time.monotonic() + max(1.0, float(budget))
    hits: list[int] = []
    lock = threading.Lock()

    def work(port: int) -> None:
        try:
            if probe(f"http://{host}:{port}", cfg):
                with lock:
                    hits.append(port)
        except Exception:
            return

    batch = 16
    for i in range(0, len(ports), batch):
        chunk = ports[i:i + batch]
        threads = [threading.Thread(target=work, args=(p,), daemon=True) for p in chunk]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=max(0.05, deadline - time.monotonic()))
        if hits:
            with lock:
                return min(hits, key=ports.index)
        if time.monotonic() >= deadline:
            LOG.debug("discovery budget exhausted while probing candidates")
            break
    return None


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

    try:
        budget = float(cfg_get(cfg, "discover.budget_seconds", 20))
    except (TypeError, ValueError):
        budget = 20.0
    deadline = time.monotonic() + max(2.0, budget)
    # netstat already told us what actually listens, so a candidate port that is
    # not in that list cannot answer. Probe the intersection first: with an engine
    # running this hits on the first try instead of after a full sweep.
    listening = _local_listen_ports()
    listening_set = set(listening)
    candidates = [p for p in cfg_get(cfg, "port_candidates", []) if p not in seen]
    live = [p for p in candidates if p in listening_set] if listening_set else candidates
    hit = _probe_first(cfg, live, max(1.0, deadline - time.monotonic()))
    if hit is not None:
        _remember_port(cfg, hit)
        return hit, "candidate"
    try:
        listen_budget = float(cfg_get(cfg, "discover.listen_budget_seconds", 4))
    except (TypeError, ValueError):
        listen_budget = 4.0
    if not listening:
        listening = _local_listen_ports()
    leftovers = [p for p in listening if p not in seen and p not in candidates]
    hit = _probe_first(cfg, leftovers, min(listen_budget, max(1.0, deadline - time.monotonic())))
    if hit is not None:
        _remember_port(cfg, hit)
        return hit, "listen_scan"
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
    """Read generation defaults from the engine instead of hard-coding them.

    Also adopts the live enumerations / limits into the workflow contract, so
    every later validation call compares against what this engine actually
    accepts rather than against a list frozen at release time.
    """
    caps = capabilities(base, cfg)
    adopted = workflow.adopt_capabilities(caps)
    # Adopt the live recipe too: size_multiple and ref_images come from it, not
    # from constants frozen at release time.
    recipe = recipe_probe(base, cfg, caps)
    d = (caps.get("defaults") or {}) if isinstance(caps, dict) else {}
    sp = d.get("sample_params") or {}
    # capabilities spells the guidance block "guided"; read it through the
    # shared contract so both names are handled and the values stay in one place
    live = workflow.defaults_from_capabilities(caps)
    return {
        "width": d.get("width"),
        "height": d.get("height"),
        "steps": live.get("steps"),
        "sampler": live.get("sampler"),
        "cfg": live.get("cfg"),
        "batch_count": d.get("batch_count") or live.get("count"),
        "workflow": live,
        "_sample_params_raw": sp,
        # v3.3.0 extras, used by the size policy and by the availability reports
        "current_mode": caps.get("current_mode") if isinstance(caps, dict) else None,
        "supported_modes": caps.get("supported_modes") if isinstance(caps, dict) else None,
        "limits": workflow.engine_limits(),
        "output_formats": list(workflow.OUTPUT_FORMATS),
        "preview_modes": list(workflow.PREVIEW_MODES),
        "upscalers": list(workflow.UPSCALERS),
        "features": dict(workflow.FEATURES),
        "model": caps.get("model") if isinstance(caps, dict) else None,
        "loras": caps.get("loras") if isinstance(caps, dict) else None,
        # v4.1 reference channel facts
        "recipe": dict(workflow.RECIPE_META),
        "size_multiple": workflow.size_multiple(cfg_get(cfg, "size.multiple", 8)),
        "ref_capable": workflow.ref_capable(),
        "recipe_probe": recipe or None,
        "adopted": adopted,
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
        "limits": d.get("limits"),
        "features": d.get("features"),
        "current_mode": d.get("current_mode"),
        "supported_modes": d.get("supported_modes"),
        "recipe": d.get("recipe"),
        "size_multiple": d.get("size_multiple"),
        "ref_capable": d.get("ref_capable"),
        "config_path": str(active_config_path()),
    }


# --------------------------------------------------------------------------
# Size policy
# --------------------------------------------------------------------------

def _size_policy(cfg: dict, gpu: dict | None, limits: dict | None = None) -> dict:
    """Two-tier size policy.

    * automatic (the tool picks the size): min_area / max_side / VRAM tiering
    * explicit  (the caller passed width/height): only the engine's own
      ``limits`` are enforced, everything else becomes a warning

    Before v3.2 the second tier did not exist and the automatic numbers were
    applied to explicit requests too, silently rewriting user input.
    """
    lim = limits if isinstance(limits, dict) and limits else workflow.engine_limits()
    eng_max_w = int(lim.get("max_width") or 4096)
    eng_max_h = int(lim.get("max_height") or 4096)
    eng_min_edge = max(int(lim.get("min_width") or 64), int(lim.get("min_height") or 64))

    smin = int(cfg_get(cfg, "size.min_area", 768 * 768))
    #: recipe first (flux2 wants 16), then the config, then 8
    mult = workflow.size_multiple(cfg_get(cfg, "size.multiple", 8))
    hard_max = min(int(cfg_get(cfg, "size.hard_max_side", 4096)), max(eng_max_w, eng_max_h))
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
    return {
        "min_area": smin,
        "min_side_hint": int(round(smin ** 0.5 / mult) * mult),
        "max_side": max_side,
        "multiple": mult,
        "auto_from_vram": auto,
        "explicit_respect": bool(cfg_get(cfg, "size.explicit_respect", True)),
        "vram_mb_per_mp": int(cfg_get(cfg, "size.vram_mb_per_mp", 1600)),
        "engine": {"max_width": eng_max_w, "max_height": eng_max_h,
                   "min_edge": eng_min_edge},
    }


def _align(v: float, mult: int) -> int:
    return max(mult, int(round(v / mult) * mult))


def normalize_size(width, height, cfg: dict, gpu: dict | None = None,
                   guard: bool = True, downscale: float = 1.0,
                   explicit: bool = False, limits: dict | None = None) -> tuple[int, int, list[str]]:
    """Align to the multiple, then apply the matching size tier.

    ``explicit=True`` (the caller named a width/height) means the request is
    honoured: only the engine's own ``limits`` can shrink it, because the engine
    rejects anything outside them with HTTP 400. Everything else -- a small
    total area, an edge above the VRAM-friendly recommendation -- comes back as
    a note, not as a silent rewrite.

    ``explicit=False`` keeps the old automatic behaviour: area floor and a
    VRAM-tiered long edge, used when the tool decided the size itself.
    """
    pol = _size_policy(cfg, gpu, limits)
    mult, min_area, max_side = pol["multiple"], pol["min_area"], pol["max_side"]
    eng = pol["engine"]
    respect = pol.get("explicit_respect", True)
    notes: list[str] = []
    try:
        w, h = int(width), int(height)
    except Exception:
        raise BridgeError(f"width/height must be integers, got {width!r}/{height!r}", "bad_arguments")
    if w <= 0 or h <= 0:
        raise BridgeError("width/height must be positive", "bad_arguments")

    if downscale and downscale != 1.0:
        w, h = w * downscale, h * downscale

    if guard and explicit and respect:
        # --- tier 1: the engine's hard limits, the only thing we may change ---
        if w < eng["min_edge"] or h < eng["min_edge"]:
            w = max(w, eng["min_edge"])
            h = max(h, eng["min_edge"])
            notes.append(f"size raised to the engine minimum edge {eng['min_edge']}px "
                         f"({int(w)}x{int(h)}); the engine rejects anything smaller")
        if w > eng["max_width"] or h > eng["max_height"]:
            scale = min(eng["max_width"] / float(w), eng["max_height"] / float(h))
            nw, nh = _align(w * scale, mult), _align(h * scale, mult)
            nw = min(nw, eng["max_width"])
            nh = min(nh, eng["max_height"])
            notes.append(f"size {int(w)}x{int(h)} is beyond the engine limits "
                         f"{eng['max_width']}x{eng['max_height']}; reduced to {nw}x{nh} "
                         "(the engine returns HTTP 400 above this)")
            w, h = nw, nh
        # --- tier 2: advisory only -------------------------------------------
        if w * h < min_area:
            notes.append(f"size {int(w)}x{int(h)} is below the {min_area} px area this engine is "
                         "stable at; honoured as requested, but small canvases sometimes fail "
                         "with 'no results' -- raise it if the job errors out")
        if max(w, h) > max_side:
            need_mp = (w * h) / 1_000_000.0
            need_mb = int(need_mp * pol.get("vram_mb_per_mp", 1600))
            msg = (f"size {int(w)}x{int(h)} exceeds the recommended long edge {max_side}px; "
                   f"honoured as requested (engine limit is {eng['max_width']}px)")
            if gpu and gpu.get("total_mb"):
                budget = int(gpu["total_mb"] * 0.75)
                if need_mb > budget:
                    msg += (f". Estimated working set ~{need_mb} MB vs ~{budget} MB usable on "
                            f"{gpu.get('name') or 'this GPU'} ({gpu['total_mb']} MB total): "
                            "expect OOM -- enable vae_tiling, lower count, or shrink the canvas")
                else:
                    msg += f". Estimated working set ~{need_mb} MB, fits in {gpu['total_mb']} MB"
            notes.append(msg)
    elif guard:
        if w * h < min_area:
            scale = (min_area / float(w * h)) ** 0.5
            nw, nh = _align(w * scale, mult), _align(h * scale, mult)
            notes.append(f"size {int(w)}x{int(h)} is below the engine's stable area; upscaled to {nw}x{nh}")
            w, h = nw, nh
        ceiling = min(max_side, max(eng["max_width"], eng["max_height"]))
        if max(w, h) > ceiling:
            scale = ceiling / float(max(w, h))
            nw, nh = _align(w * scale, mult), _align(h * scale, mult)
            notes.append(f"size {int(w)}x{int(h)} exceeds the safe long edge {ceiling}px; reduced to {nw}x{nh}")
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

# --------------------------------------------------------------------------
# Reference images -- LocModel 3.6.x ``ref_images`` channel
# --------------------------------------------------------------------------
#: accepted values for ``ref_fit``
REF_FITS = ("follow_first", "image")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MIME_BY_SUFFIX = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                   ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}

#: recipes list per engine base (~150 KB); the model changes rarely, the port does
_RECIPE_CACHE: dict = {}


def image_size(data: bytes):
    """Width/height from PNG / JPEG / GIF / WebP / BMP bytes, without Pillow."""
    try:
        if data[:8] == _PNG_MAGIC and len(data) >= 24:
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            return (w, h) if w and h else None
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            w = int.from_bytes(data[6:8], "little")
            h = int.from_bytes(data[8:10], "little")
            return (w, h) if w and h else None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            if chunk == b"VP8 ":
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return (w, h) if w and h else None
            if chunk == b"VP8L" and len(data) >= 25:
                bits = int.from_bytes(data[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        if data[:2] == b"BM" and len(data) >= 26:
            w = int.from_bytes(data[18:22], "little", signed=True)
            h = abs(int.from_bytes(data[22:26], "little", signed=True))
            return (w, h) if w and h else None
        if data[:2] == b"\xff\xd8":  # JPEG: walk the segment chain to a SOFn frame
            i, n = 2, len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seglen = int.from_bytes(data[i + 2:i + 4], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return (w, h) if w and h else None
                i += 2 + max(2, seglen)
    except Exception:  # noqa: BLE001 - a malformed header only means "size unknown"
        return None
    return None


def _sniff_mime(data: bytes) -> str:
    if data[:8] == _PNG_MAGIC:
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    raise BridgeError("reference is not a PNG / JPEG / WebP / GIF / BMP image",
                      "bad_arguments", "convert it first (any editor can re-export as PNG)")


def _b64_to_bytes(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=False)
    except Exception:  # noqa: BLE001
        return b""


def as_ref_uri(src: str, cfg: dict) -> dict:
    """Normalise one reference into a data URI plus the facts we can verify.

    Three input shapes are accepted because three callers exist: agents have a
    path, code often has a data URI, and MCP clients usually paste bare base64.
    """
    if not isinstance(src, str) or not src.strip():
        raise BridgeError("each reference must be a non-empty string", "bad_arguments")
    text = src.strip()
    limit = int(cfg_get(cfg, "ref.max_mb", 8)) * 1024 * 1024
    source, mime, data = "path", "", b""

    if text[:5].lower() == "data:":
        head, _, b64 = text.partition(",")
        mime = head[5:].split(";")[0].strip().lower()
        data = _b64_to_bytes(b64)
        source = "data_uri"
        if not data:
            raise BridgeError("reference data URI carries no decodable payload",
                              "bad_arguments")
    else:
        p = Path(text).expanduser()
        if p.is_file():
            size = p.stat().st_size
            if size > limit:
                raise BridgeError(f"reference {p.name} is {size} bytes, over the "
                                  f"{limit} byte budget (config ref.max_mb)",
                                  "bad_arguments", "shrink the image or raise ref.max_mb")
            data = p.read_bytes()
            mime = _MIME_BY_SUFFIX.get(p.suffix.lower(), "")
        else:
            data = _b64_to_bytes(text)
            source = "base64"
            if not data:
                raise BridgeError(f"reference not found: {p}", "bad_arguments",
                                  "pass an absolute path, a data URI, or raw base64")

    if len(data) > limit:
        raise BridgeError(f"reference payload is {len(data)} bytes, over the {limit} byte "
                          "budget (config ref.max_mb)", "bad_arguments")
    if not mime or not mime.startswith("image/"):
        mime = _sniff_mime(data)
    w, h = image_size(data) or (0, 0)
    return {"uri": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
            "bytes": len(data), "width": w or None, "height": h or None,
            "source": source, "mime": mime}


def ref_plan(ref_images, cfg: dict) -> dict:
    """Validate the list and normalise every entry (raises on misuse)."""
    if isinstance(ref_images, str):
        ref_images = [ref_images]
    items = [s for s in (ref_images or []) if isinstance(s, str) and s.strip()]
    maxn = int(cfg_get(cfg, "ref.max_images", 8))
    if len(items) > maxn:
        raise BridgeError(f"{len(items)} reference images exceed the maximum {maxn}",
                          "bad_arguments",
                          "use reference #1 as the identity anchor and drop the rest")
    prepared = [as_ref_uri(s, cfg) for s in items]
    return {"items": prepared, "uris": [p["uri"] for p in prepared]}


def ref_canvas(size, max_pixels: int, multiple: int):
    """Fit reference #1 into the pixel budget while keeping its aspect ratio."""
    w, h = size or (0, 0)
    if not w or not h:
        side = max(multiple, int(round((max_pixels ** 0.5) / multiple) * multiple))
        return side, side, [f"the first reference reports no size; a {side}x{side} canvas "
                            "was used instead"]
    ratio = w / float(h)
    hh = (max_pixels / ratio) ** 0.5
    aw, ah = _align(hh * ratio, multiple), _align(hh, multiple)
    while aw * ah > max_pixels and aw > multiple and ah > multiple:
        aw -= multiple
        ah -= multiple
    notes = [f"canvas {aw}x{ah} derived from the first reference ({w}x{h}, "
             f"aspect {ratio:.3f}) within the {max_pixels} px budget; pass width/height "
             "with ref_fit=\"image\" to keep your own size"]
    return aw, ah, notes


def _engine_trace(img: dict) -> dict:
    """What the engine actually built, read back from its own report.

    ``generation_info`` carries the built workflow: a ``fn ...`` signature per
    helper and the predictor call. On the ref channel that call lists
    ``ref_latents``; when it is absent the reference was dropped and the picture
    is plain text-to-image -- the exact failure mode this release makes visible.
    """
    info = (img or {}).get("generation_info") or ""
    if not info:
        return {}
    fn = ""
    fallback_fn = ""
    call = ""
    for line in info.splitlines():
        text = line.strip()
        if text.startswith("fn "):
            if "fn main(" in text:
                fn = text[:160]
            elif not fallback_fn:
                fallback_fn = text[:160]
        elif "predictor(" in text and "ref_latents" in text:
            call = text[:400]
    bound = re.findall(r"reference_latent_(\d+)", call) or re.findall(r"reference_latent_(\d+)", info)
    return {"fn": fn or fallback_fn, "ref_latents": bool(call) or bool(bound),
            "reference_count": len(set(bound)) if bound else 0, "call": call}


def _ref_summary(refs: dict, fit: str, budget: int) -> dict:
    return {"channel": "ref_images", "count": len(refs["uris"]), "fit": fit,
            "max_pixels": budget,
            "sources": [{k: v for k, v in it.items() if k != "uri"}
                        for it in refs["items"]]}


def _model_hint(obj) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for key in ("filename", "name", "model", "id", "title"):
            if obj.get(key):
                return str(obj[key])
    return ""


def recipe_probe(base: str, cfg: dict, caps: dict | None = None,
                 refresh: bool = False) -> dict:
    """Read the running recipe out of ``/sdcpp/v1/recipes`` and adopt its facts.

    LocModel 3.6.x rounds every requested size to the recipe's ``size_multiple``
    (16 for flux2), so a caller that does not pre-align reports a canvas the
    engine never rendered. The endpoint does not say which recipe is live, so we
    match it against the model the capabilities report.
    """
    path = cfg_get(cfg, "engine.recipes_path", "/sdcpp/v1/recipes")
    recs = None if refresh else _RECIPE_CACHE.get(base)
    if recs is None:
        code, obj = None, None
        try:
            code, obj = http_json(base + path, timeout=15)
        except Exception as exc:  # noqa: BLE001 - an older engine simply has no recipes
            LOG.debug("recipes probe failed: %s", exc)
        recs = obj.get("recipes") if isinstance(obj, dict) else obj
        if code != 200 or not isinstance(recs, list):
            recs = []
        _RECIPE_CACHE[base] = recs

    caps = caps if isinstance(caps, dict) else {}
    runtime = caps.get("runtime") if isinstance(caps.get("runtime"), dict) else {}
    rr = runtime.get("runtime_recipe") if isinstance(runtime.get("runtime_recipe"), dict) else {}
    hints = set()
    for raw in (caps.get("model"), rr.get("id"), rr.get("model"), rr.get("name")):
        name = _model_hint(raw) if raw is not None else ""
        if name:
            hints.add(workflow._squash(Path(name).stem))
            hints.add(workflow._squash(name))

    meta: dict = {}
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        keys = set()
        for field in ("id", "name", "model", "file", "filename", "path"):
            val = rec.get(field)
            if val:
                keys.add(workflow._squash(Path(str(val)).stem))
                keys.add(workflow._squash(str(val)))
        if not (keys & hints):
            continue
        feats = rec.get("effective_features") or rec.get("features") or {}
        meta = {"id": rec.get("id") or rec.get("name") or rec.get("file"),
                "size_multiple": rec.get("size_multiple"),
                "defaults": rec.get("defaults"),
                "slots": rec.get("slots"),
                "modes": rec.get("modes")}
        if isinstance(feats, dict) and "ref_images" in feats:
            meta["ref_images"] = bool(feats.get("ref_images"))
        break
    if not meta and rr:
        meta = {"id": rr.get("id"), "size_multiple": rr.get("size_multiple"),
                "ref_images": rr.get("ref_images")}
    meta = {k: v for k, v in meta.items() if v is not None}
    if meta:
        workflow.set_recipe_meta(meta)
    return meta



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
             params: dict | None = None, dry_run: bool = False,
             ref_images: list[str] | str | None = None, ref_fit: str | None = None,
             ref_max_pixels: int | None = None,
             ref_strength: float | None = None) -> dict:
    cfg = cfg or load_config()
    if not prompt or not prompt.strip():
        raise BridgeError("prompt must not be empty", "bad_arguments")

    # Validate cheap arguments BEFORE touching the engine so that misuse never
    # costs a network round-trip.
    count = 1 if count is None else count
    cfg_max_count = int(cfg_get(cfg, "generate.max_count", 8))
    eng_batch_max = int(workflow.LIMITS.get("max_batch_count") or 0)
    max_count = min(cfg_max_count, eng_batch_max) if eng_batch_max else cfg_max_count
    if int(count) > max_count:
        raise BridgeError(
            f"count={count} exceeds the maximum batch count {max_count} "
            f"(config generate.max_count={cfg_max_count}, engine limits.max_batch_count="
            f"{eng_batch_max or 'unknown'})",
            "bad_arguments")
    if ref_strength is not None:
        # Measured on LocModel 3.6.5: the ref workflow never calls
        # slice_sigmas_by_strength, so strength is silently inert. Refusing is
        # kinder than accepting a knob that does nothing.
        raise BridgeError(
            "ref_strength is not supported: on the ref_images channel the engine ignores "
            "strength entirely (verified: the workflow never slices sigmas). Use "
            "redraw_image (init_image + strength) when you need a denoising strength.",
            "unsupported_parameter")
    for name, val in (("width", width), ("height", height)):
        if val is not None and int(val) <= 0:
            raise BridgeError(f"{name} must be a positive integer, got {val}", "bad_arguments")
    if aspect is not None and aspect not in ASPECTS:
        raise BridgeError(f"unknown aspect {aspect!r}; supported: {', '.join(ASPECTS)}", "bad_arguments")

    base, port, src = base_url(cfg=cfg)
    gpu = gpu_memory()
    eng = engine_defaults(base, cfg)

    # --- v3.1 workflow parameters ------------------------------------------
    # Payload-only by design: LocModel owns config.json and rewrites custom
    # values from its own panel state, so anything stored there is volatile.
    _explicit: dict = {}
    if steps is not None:
        _explicit["steps"] = steps
    if sampler:
        _explicit["sampler"] = sampler
    if cfg_scale is not None:
        _explicit["cfg"] = cfg_scale
    if seed not in (None, -1):
        _explicit["seed"] = seed
    try:
        wf = workflow.resolve(_explicit, workflow.normalize(params), workflow.session_get())
    except workflow.ParamError as exc:
        raise BridgeError(str(exc), "bad_arguments", getattr(exc, "hint", "")) from None

    # An explicit size means the caller already decided; only the engine's own
    # limits may override it (see normalize_size). "explicit" excludes sizes we
    # derived from an aspect preset, which the tool owns.
    explicit_size = bool(width or height) and not aspect
    eng_limits = eng.get("limits") if isinstance(eng.get("limits"), dict) else None

    # The engine may report a tighter batch ceiling than the one we assumed
    # before probing it; re-check now that limits are real.
    eng_batch_max = int((eng_limits or {}).get("max_batch_count") or workflow.LIMITS.get("max_batch_count") or 0)
    if eng_batch_max:
        max_count = min(cfg_max_count, eng_batch_max)
        if int(count) > max_count:
            raise BridgeError(
                f"count={count} exceeds the maximum batch count {max_count} "
                f"(config generate.max_count={cfg_max_count}, engine limits.max_batch_count="
                f"{eng_batch_max})",
                "bad_arguments")

    # --- v4.1 reference-image channel --------------------------------------
    # LocModel 3.6.x binds ref_images at step 0 and takes a *plural* key only:
    # a singular ref_image is dropped silently and the job degrades into plain
    # text-to-image. Everything below is normalisation plus honest reporting.
    ref_sources = ref_images if ref_images is not None else wf.pop("ref_images", None)
    if ref_images is not None:
        wf.pop("ref_images", None)
    fit = ref_fit or wf.pop("ref_fit", None) or cfg_get(cfg, "ref.fit", "follow_first")
    fit = str(fit).strip().lower().replace("-", "_") if isinstance(fit, str) else "follow_first"
    if fit in ("first", "follow", "followfirst"):
        fit = "follow_first"
    if fit not in REF_FITS:
        raise BridgeError(f"unknown ref_fit {ref_fit!r}; supported: {', '.join(REF_FITS)}",
                          "bad_arguments")
    budget = ref_max_pixels if ref_max_pixels is not None else wf.pop("ref_max_pixels", None)
    if budget is None:
        budget = cfg_get(cfg, "ref.max_pixels", 1024 * 1024)
    ceiling = int(cfg_get(cfg, "ref.max_pixels_ceiling", 4 * 1024 * 1024))
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        raise BridgeError(f"ref_max_pixels must be an integer, got {budget!r}",
                          "bad_arguments") from None
    if budget < 65536 or budget > ceiling:
        raise BridgeError(
            f"ref_max_pixels {budget} is outside the supported range 65536-{ceiling}",
            "bad_arguments",
            "1048576 (1024x1024) is the default; 1570000 px was measured safe on "
            "flux-2-klein-4b, well above that the engine runs out of VRAM")
    refs = ref_plan(ref_sources, cfg) if ref_sources else {"items": [], "uris": []}
    ref_active = bool(refs["uris"])
    ref_notes: list[str] = []
    if ref_active:
        if workflow.ref_capable() is False:
            ref_notes.append("the loaded recipe does not advertise ref_images; the request is "
                             "sent anyway, but verify the result before relying on it")
        if fit == "image" and not explicit_size and not aspect:
            raise BridgeError('ref_fit="image" needs an explicit width/height (or an aspect preset)',
                              "bad_arguments",
                              "omit ref_fit to derive the canvas from the first reference")
        for dead in ("strength", "init_image", "mask_image"):
            if dead in wf:
                wf.pop(dead, None)
                ref_notes.append(f"{dead} was dropped: the ref_images channel ignores it "
                                 "(use redraw_image when you need init_image/strength)")
        if not explicit_size and not aspect:
            first = refs["items"][0]
            cw, ch, cn = ref_canvas((first["width"], first["height"]), budget,
                                    workflow.size_multiple(cfg_get(cfg, "size.multiple", 8)))
            width, height = cw, ch
            ref_notes.extend(cn)
        elif aspect:
            ref_notes.append("the aspect preset wins over the reference framing; pass "
                             "ref_fit=\"follow_first\" without an aspect to match the reference")
        if (negative_prompt or "").strip():
            ref_notes.append("negative_prompt is accepted for compatibility, but the "
                             "ref_images channel never applies it")

    if aspect and not (width and height):
        w0, h0 = aspect_to_size(aspect, (width or height or 1024) ** 2 or 1024 * 1024)
        width, height = w0, h0
    width = int(width or eng.get("width") or 1024)
    height = int(height or eng.get("height") or 1024)
    steps = int(steps if steps is not None else (eng.get("steps") or workflow.FALLBACK["steps"]))
    sampler = sampler or eng.get("sampler") or workflow.FALLBACK["sampler"]
    if cfg_scale is None:
        cfg_scale = eng.get("cfg") if eng.get("cfg") is not None else workflow.FALLBACK["cfg"]

    if seed is None or int(seed) < 0:
        seed = random.randint(1, 2 ** 31 - 1)
    seed = int(seed)
    timeout = int(timeout or cfg_get(cfg, "generate.timeout", 900))
    retry = int(retry if retry is not None else cfg_get(cfg, "generate.retry", 2))
    poll = float(cfg_get(cfg, "generate.poll_interval", 2.0))

    w, h, notes = normalize_size(width, height, cfg, gpu, guard=guard,
                                 explicit=explicit_size, limits=eng_limits)
    notes[:0] = ref_notes
    if wf:
        inert = workflow.unsupported_keys(list(wf))
        if inert:
            notes.append("the loaded model does not expose " + ", ".join(inert) +
                         "; the values were sent but will have no effect")
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
    # On the ref channel the engine runs its own guidance (3.5 on flux2-klein),
    # so only send ours when the caller actually asked for a value.
    if cfg_scale is not None and (not ref_active or "cfg" in _explicit):
        payload["sample_params"]["guidance"] = {"txt_cfg": float(cfg_scale)}
    if model:
        payload["model"] = model
    if ref_active:
        # The plural key is what turns the channel on; nothing else in the
        # payload can substitute for it.
        payload["ref_images"] = refs["uris"]

    if wf:
        workflow.apply(payload, wf)
    payload.setdefault("sample_params", {})
    steps = int(payload["sample_params"].get("sample_steps") or steps)
    sampler = payload["sample_params"].get("sample_method") or sampler
    cfg_scale = (payload["sample_params"].get("guidance") or {}).get("txt_cfg", cfg_scale)
    if ref_active and "guidance" not in (payload.get("sample_params") or {}):
        cfg_scale = None  # the engine's own default is in force; do not report a guess
    if ref_active:
        warn_steps = int(cfg_get(cfg, "ref.steps_warn", 12))
        if int(payload["sample_params"].get("sample_steps") or steps) > warn_steps:
            notes.append(
                f"steps={steps} on the ref_images channel: the reference is bound at step 0, "
                "so extra steps add no fidelity (4-8 is enough; 4 steps measured ~3 s here)")
    if "count" in wf:
        count = int(wf["count"])
    if int(payload.get("seed", seed)) < 0:
        payload["seed"] = random.randint(1, 2 ** 31 - 1)
    seed = int(payload.get("seed", seed))

    out_path = Path(out_dir or cfg_get(cfg, "output.dir") or (Path.cwd() / "output"))

    if dry_run:
        brief = dict(payload)
        if ref_active:
            # Printing megabytes of base64 helps nobody; show the shape instead.
            brief["ref_images"] = [
                f"<ref {i + 1}: {it['bytes']} bytes, {it['width'] or '?'}x"
                f"{it['height'] or '?'}, via {it['source']}>"
                for i, it in enumerate(refs["items"])]
        return {"ok": True, "dry_run": True, "payload": brief, "size_notes": notes,
                "ref": _ref_summary(refs, fit, budget) if ref_active else None,
                "engine": {"base": base, "port": port, "detected_by": src},
                "out_dir": str(out_path), "engine_defaults": eng,
                "size_policy": _size_policy(cfg, gpu, limits=eng_limits)}

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

    if ref_active and images:
        trace = _engine_trace(images[0])
        if trace and not trace.get("ref_latents"):
            notes.append(
                "WARNING: the engine reports no ref_latents, so this result is plain "
                "text-to-image and the subject will not be preserved -- check the model "
                "(flux-2-klein or newer) and that ref_images reached the payload")
        elif trace.get("ref_latents"):
            notes.append(f"engine bound {trace.get('reference_count') or len(refs['items'])} "
                         f"reference latent(s): {trace.get('fn') or 'ref channel active'}")

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
        files.append({"path": str(fp.resolve()), "bytes": len(data),
                      "engine_trace": _engine_trace(img)})

    meta = {
        "app": APP_NAME, "version": VERSION, "prompt": payload["prompt"],
        "workflow": wf,
        "payload_sample_params": payload.get("sample_params"),
        "negative_prompt": payload["negative_prompt"], "width": payload["width"],
        "height": payload["height"], "steps": steps, "sampler": sampler,
        "cfg": cfg_scale, "seed": seed, "count": count,
        "model": current_model(base, cfg), "job_id": job_id, "port": port,
        "ref": _ref_summary(refs, fit, budget) if ref_active else None,
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
                   "seed": seed, "count": count, "workflow": wf},
        "model": meta["model"], "attempts": attempts, "notes": notes,
        "ref": _ref_summary(refs, fit, budget) if ref_active else None,
        "engine_trace": (files[0]["engine_trace"] if files else {}),
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
        ref_state = eng.get("ref_capable")
        add("reference_images", ref_state is not False,
            "ref_images supported by the loaded recipe" if ref_state else
            ("ref_images state unknown (the engine reports no recipe detail)"
             if ref_state is None else
             "the loaded recipe does not expose ref_images: a ref request falls back "
             "to plain text-to-image"),
            "warn" if ref_state is False else "info")
        add("recipe", bool(workflow.RECIPE_META),
            json.dumps(workflow.RECIPE_META, ensure_ascii=False) if workflow.RECIPE_META
            else "no recipe matched (engine older than 3.6, or /sdcpp/v1/recipes unavailable)",
            "warn" if not workflow.RECIPE_META else "info")

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


def _parse_param_list(items: list[str] | None) -> dict:
    """Turn repeated ``KEY=VALUE`` CLI arguments into a mapping."""
    out: dict = {}
    for raw in items or []:
        if "=" not in raw:
            raise BridgeError(f"--param expects KEY=VALUE, got {raw!r}", "bad_arguments",
                              "example: --param cfg=3.5")
        key, val = raw.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `video` / `lipsync` are forwarded before argparse: those bridges own their
    # own sub-command parsers and must not be squeezed through this one.
    if argv and argv[0] in ("video", "lipsync"):
        name, rest = argv[0], argv[1:]
        try:
            if name == "video":
                import locmodel_video as _video
                return _video.cli(rest)
            import locmodel_lipsync as _lipsync
            return _lipsync._cli([name] + rest)
        except ImportError as exc:  # pragma: no cover - packaging safety net
            print(json.dumps({"ok": False, "error": "module_missing",
                              "message": f"{name} bridge unavailable: {exc}"},
                             ensure_ascii=False, indent=2))
            return 2

    ap = argparse.ArgumentParser(
        prog="locmodel_image.py",
        description=f"LocModel / stable-diffusion.cpp image bridge v{VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("forwarded sub-commands (own parsers, so not listed above):\n"
                "  video status|generate|job|cancel|recipes"
                "   -> 视频选项卡 → 生成视频（引擎 vid_gen）\n"
                "  lipsync status|run"
                "                   -> 视频选项卡 → 对口型视频（LocModel 独立管线）\n"
                "注意：对口型视频需要先在 LocModel 里「一键准备」下载并挂载专用模型与运行环境，"
                "未挂载将导致整体任务失败，没有降级路径。"),
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
    pr = sub.add_parser("params", help="show / set / reset workflow parameters")
    pr.add_argument("action", nargs="?", default="list",
                    choices=["list", "show", "set", "reset"],
                    help="list (default) | set with --param | reset everything")
    pr.add_argument("--param", action="append", metavar="KEY=VALUE")
    pr.add_argument("--all", action="store_true", help="include folded parameter groups")

    g = sub.add_parser("generate", add_help=False,
                       help="submit a generation job and wait for the result")
    g.add_argument("-h", "--help", action="help", help="show this help message and exit")
    g.add_argument("--prompt", required=True)
    g.add_argument("--negative", default="")
    g.add_argument("--width", type=int)
    g.add_argument("--height", type=int)
    g.add_argument("--aspect", help=f"aspect preset: {', '.join(ASPECTS)}")
    g.add_argument("--steps", type=int)
    g.add_argument("--sampler")
    g.add_argument("--cfg", type=float, dest="cfg_scale")
    g.add_argument("--seed", type=int, default=-1)
    g.add_argument("--count", type=int)
    g.add_argument("--param", action="append", metavar="KEY=VALUE",
                   help="workflow parameter override, repeatable "
                        "(e.g. --param cfg=3.5 --param scheduler=karras)")
    g.add_argument("--model", help="model name override (multi-model engines)")
    g.add_argument("--out", dest="out_dir")
    g.add_argument("--prefix", default="locmodel")
    g.add_argument("--timeout", type=int)
    g.add_argument("--retry", type=int)
    g.add_argument("--ref-image", action="append", metavar="PATH|DATA_URI|BASE64",
                   help="reference image for the ref_images channel; repeatable, the "
                        "first one anchors identity")
    g.add_argument("--ref-fit", choices=list(REF_FITS),
                   help="follow_first (default) derives the canvas from the first "
                        "reference; image keeps --width/--height")
    g.add_argument("--ref-max-pixels", type=int,
                   help="pixel budget for an auto-derived canvas (default 1048576)")
    g.add_argument("--no-guard", action="store_true", help="force tier-A sizing: send the requested size as-is, only the engine limits may clamp it")
    g.add_argument("--dry-run", action="store_true", help="print the payload without generating")

    # Same arguments as ``generate``, but the reference channel is the point rather than an
    # option: ``--ref-image`` becomes mandatory and the help text says so.
    gx = sub.add_parser("ref-edit", parents=[g], add_help=False,
                        help="edit by reference: keep the subject, change only what the prompt names")

    e = sub.add_parser("edit", help="repaint only a masked area of an existing image")
    e.add_argument("--image", required=True, help="image to modify")
    e.add_argument("--prompt", required=True, help="what should appear inside the mask")
    e.add_argument("--mask", help="mask PNG (white = repaint)")
    e.add_argument("--region", action="append", metavar="x1,y1,x2,y2",
                   help="rect region; repeatable. Ignored when --mask is given")
    e.add_argument("--unit", choices=["norm", "px"], default="norm",
                   help="region coordinates: norm (0..1, default) or px")
    e.add_argument("--invert", action="store_true", help="invert the generated mask")
    e.add_argument("--strength", type=float, help="denoising strength (default 0.75)")
    e.add_argument("--negative", default="")
    e.add_argument("--model")
    e.add_argument("--steps", type=int)
    e.add_argument("--sampler")
    e.add_argument("--cfg", type=float, dest="cfg_scale")
    e.add_argument("--seed", type=int, default=-1)
    e.add_argument("--mask-blur", type=int, dest="mask_blur")
    e.add_argument("--padding", type=int, dest="inpaint_padding")
    e.add_argument("--channel", choices=["auto", "img_gen", "img2img"])
    e.add_argument("--out", dest="out_dir")
    e.add_argument("--prefix", default="locmodel-edit")
    e.add_argument("--timeout", type=int)
    e.add_argument("--verify", choices=["agent", "off"])
    e.add_argument("--dry-run", action="store_true")

    r = sub.add_parser("redraw", help="re-draw the whole image from a new prompt")
    r.add_argument("--image", required=True)
    r.add_argument("--prompt", required=True)
    r.add_argument("--strength", type=float, help="denoising strength (default 0.6)")
    r.add_argument("--negative", default="")
    r.add_argument("--model")
    r.add_argument("--steps", type=int)
    r.add_argument("--sampler")
    r.add_argument("--cfg", type=float, dest="cfg_scale")
    r.add_argument("--seed", type=int, default=-1)
    r.add_argument("--channel", choices=["auto", "img_gen", "img2img"])
    r.add_argument("--out", dest="out_dir")
    r.add_argument("--prefix", default="locmodel-redraw")
    r.add_argument("--timeout", type=int)
    r.add_argument("--verify", choices=["agent", "off"])
    r.add_argument("--dry-run", action="store_true")

    em = sub.add_parser("edit-models", help="models that can edit, with priority and auto pick")
    em.add_argument("--refresh", action="store_true",
                    help="ignore the cached model snapshot and probe the engine again")

    lm = sub.add_parser("load-model", help="load / switch the model in the engine")
    lm.add_argument("model", help="model name or filename")
    lm.add_argument("--timeout", type=int, default=240)

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
        elif args.cmd == "params":
            base_p, port_p, src_p = base_url(cfg=cfg)
            caps_p = capabilities(base_p, cfg) if port_p else {}
            act = args.action
            if act in ("list", "show"):
                _emit({"ok": True, "version": VERSION,
                       "engine": {"port": port_p, "detected_by": src_p},
                       "core": workflow.table(caps_p, workflow.session_get(),
                                              include_folded=args.all),
                       "folded_groups": workflow.FOLDED_GROUPS,
                       "session": workflow.session_get(),
                       "note": "defaults come from the engine (/sdcpp/v1/capabilities); "
                               "source=engine means live, source=tool means a built-in fallback"})
                return 0
            if act == "set":
                _emit({"ok": True, "session": workflow.session_set(
                    _parse_param_list(args.param))})
                return 0
            _emit({"ok": True, "reset": True, "session": workflow.session_reset()})
            return 0
        elif args.cmd in ("generate", "ref-edit"):
            if args.cmd == "ref-edit" and not args.ref_image:
                raise BridgeError(
                    "ref-edit needs at least one --ref-image", "bad_arguments",
                    'use "generate" for text-to-image, or pass --ref-image PATH')
            res = generate(
                args.prompt, cfg,
                negative_prompt=args.negative, width=args.width, height=args.height,
                aspect=args.aspect, steps=args.steps, sampler=args.sampler,
                cfg_scale=args.cfg_scale, seed=args.seed, count=args.count,
                out_dir=args.out_dir, filename_prefix=args.prefix,
                timeout=args.timeout, retry=args.retry, guard=not args.no_guard,
                model=args.model, params=_parse_param_list(args.param),
                dry_run=args.dry_run, ref_images=args.ref_image,
                ref_fit=args.ref_fit, ref_max_pixels=args.ref_max_pixels,
            )
            _emit(res)
            return 0 if res.get("ok") else _EXIT["generation_failed"]
        elif args.cmd in ("edit", "redraw", "edit-models", "load-model"):
            import locmodel_edit as ed  # local import: locmodel_edit imports this module
            if args.cmd == "edit-models":
                _emit(ed.list_models(cfg, refresh=getattr(args, "refresh", False)))
            elif args.cmd == "load-model":
                _emit(ed.load_model(cfg, args.model, timeout=args.timeout))
            else:
                common = dict(
                    negative_prompt=args.negative, model=args.model, steps=args.steps,
                    sampler=args.sampler, cfg_scale=args.cfg_scale, seed=args.seed,
                    channel=args.channel, out_dir=args.out_dir,
                    filename_prefix=args.prefix, timeout=args.timeout,
                    verify=args.verify, strength=args.strength, dry_run=args.dry_run,
                )
                if args.cmd == "edit":
                    regions = None
                    if args.region:
                        boxes = []
                        for raw in args.region:
                            nums = [float(v) for v in re.split(r"[,\s]+", raw.strip()) if v]
                            if len(nums) != 4:
                                raise BridgeError(
                                    f'--region needs 4 numbers, got "{raw}"', "bad_arguments")
                            boxes.append({"type": "rect", "x": nums})
                        regions = {"unit": args.unit, "boxes": boxes,
                                   "invert": bool(args.invert)}
                    res = ed.edit_image(args.image, args.prompt, cfg,
                                        mask_image=args.mask, regions=regions,
                                        mask_blur=args.mask_blur,
                                        inpaint_padding=args.inpaint_padding,
                                        **common)
                else:
                    res = ed.redraw_image(args.image, args.prompt, cfg, **common)
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
        code = getattr(exc, "code", None)
        if code:  # EditError and friends carry a machine-readable code
            _emit({"ok": False, "error": code, "message": str(exc),
                   "hint": getattr(exc, "hint", "")})
            return _EXIT.get(code, 1)
        _emit({"ok": False, "error": type(exc).__name__, "message": str(exc)})
        return _EXIT["generic_error"]
    return 0


if __name__ == "__main__":
    sys.exit(main())
