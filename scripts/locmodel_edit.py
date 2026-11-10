#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LocModel image EDIT bridge (v3.0.0) -- part of locmodel-image.

Adds image-to-image capability on top of the v2 generation bridge:

* ``list_edit_models``  -- merged model pool (engine list + on-disk scan + states)
* ``edit_image``        -- localised redraw (inpaint) with a mask, no extra model
* ``redraw_image``      -- whole-image restyle from a reference image
* ``load_model``        -- switch the model the engine has loaded

Design constraints inherited from v2
------------------------------------
* Zero third-party dependencies (no Pillow, no numpy): PNG encode/decode,
  JPEG header probe with the standard library only.
* The engine is never trusted to preserve pixels: every edit is recomposed on
  the CPU side with ``result = where mask: edited else original``, so "outside
  the mask nothing changes" is a guarantee of this layer, not a hope.
* Behaviour is explicit: no silent model substitution, no silent channel
  switching without recording it in the result.

Channels
--------
``img_gen`` (async, preferred)   POST /sdcpp/v1/img_gen  {init_image, mask_image, ...}
``img2img`` (sync, fallback)     POST /sdapi/v1/img2img   {init_images, mask, ...}

The async job API is preferred because a synchronous image-to-image call can
block for minutes with no way to observe progress; jobs are polled and can be
cancelled. ``edit.channel`` = ``auto`` tries the async path first and remembers
what worked in the config file.
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import struct
import time
import zlib
from datetime import datetime
from pathlib import Path

try:
    import locmodel_image as core
except Exception as exc:  # pragma: no cover - surfaced to the caller
    core = None
    _CORE_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

VERSION = "3.0.0"
PNG_SIG = b"\x89PNG\r\n\x1a\n"

# ---------------------------------------------------------------------------
# Model families
# ---------------------------------------------------------------------------
# (key, family, rank, can_region_edit, pattern)
# Ordered: the first pattern that matches wins, so "qwen-image-edit" must be
# tested before "qwen-image" (they differ by three characters and by a lot of
# behaviour).
FAMILIES = [
    ("qwen-image-edit", "edit", 1, True,
     r"qwen[\-_. ]?image[\-_. ]?edit|qwen[\-_. ]?edit"),
    ("flux2-klein", "edit", 2, True,
     r"flux[\-_. ]?2[\-_. ]?(klein|dev|pro)|flux2"),
    ("qwen-image", "edit-ref", 3, False,
     r"qwen[\-_. ]?image(?![\-_. ]?edit)"),
    ("z-image", "gen", 99, False, r"z[\-_. ]?image"),
    ("flux1", "gen", 99, False, r"flux[\-_. ]?1"),
    ("sdxl", "gen", 99, False, r"sd[\-_ ]?xl"),
    ("sd15", "gen", 99, False, r"sd[\-_ ]?1[\-_.]?5|sd15|sd21|sd31"),
]

DEFAULT_EDIT_PRIORITY = ["qwen-image-edit", "flux2-klein", "qwen-image"]
MODEL_EXTS = (".gguf", ".safetensors", ".ckpt", ".pt", ".pth", ".bin")


class EditError(RuntimeError):
    def __init__(self, message: str, code: str = "edit_failed", hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


def _need_core():
    if core is None:
        raise EditError(f"locmodel_image.py could not be imported: {_CORE_IMPORT_ERROR}",
                        "missing_core",
                        "Keep locmodel_edit.py next to locmodel_image.py.")
    return core


def _cfg(cfg):
    return _need_core().load_config() if cfg is None else cfg


def log(msg: str) -> None:
    if core is not None:
        core.LOG.debug(msg)


# ---------------------------------------------------------------------------
# Minimal PNG codec (standard library only)
# ---------------------------------------------------------------------------

def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png_gray(path: Path, width: int, height: int, pixels: bytes) -> Path:
    """Write an 8-bit greyscale PNG (mode L). ``pixels`` is width*height bytes."""
    if len(pixels) != width * height:
        raise EditError(f"pixel buffer is {len(pixels)} bytes, expected {width * height}",
                        "bad_arguments")
    raw = bytearray()
    stride = width
    for y in range(height):
        raw.append(0)                                    # filter: None
        raw += pixels[y * stride:(y + 1) * stride]
    blob = (PNG_SIG
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


def png_header(path: Path) -> dict:
    """Read width/height/bit depth/colour type without decoding pixel data."""
    with open(path, "rb") as fh:
        head = fh.read(33)
    if head[:8] != PNG_SIG:
        raise EditError(f"{path} is not a PNG file", "bad_arguments",
                        "Only PNG masks are accepted; export the mask as PNG.")
    w, h = struct.unpack(">II", head[16:24])
    depth, ctype, _comp, _filt, interlace = head[24:29]
    if interlace:
        raise EditError(f"{path} uses Adam7 interlacing, which is not supported",
                        "bad_arguments", "Re-save the mask as a non-interlaced PNG.")
    return {"width": w, "height": h, "depth": depth, "color_type": ctype}


_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


def _unfilter(raw: bytes, width: int, height: int, channels: int) -> bytearray:
    stride = width * channels
    bpp = channels
    out = bytearray(stride * height)
    prev = bytearray(stride)
    pos = 0
    o = 0
    for _y in range(height):
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ft == 0:
            pass
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        else:
            raise EditError(f"unknown PNG filter type {ft}", "bad_arguments")
        out[o:o + stride] = line
        o += stride
        prev = line
    return out


def read_png(path: Path) -> dict:
    """Decode an 8-bit PNG into raw pixels (channels = 1/2/3/4)."""
    blob = Path(path).read_bytes()
    if blob[:8] != PNG_SIG:
        raise EditError(f"{path} is not a PNG file", "bad_arguments")
    pos = 8
    idat = bytearray()
    header = None
    while pos < len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        tag = blob[pos + 4:pos + 8]
        data = blob[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            header = data
        elif tag == b"IDAT":
            idat += data
        elif tag == b"IEND":
            break
    if header is None:
        raise EditError(f"{path} has no IHDR chunk", "bad_arguments")
    w, h = struct.unpack(">II", header[:8])
    depth, ctype, _c, _f, interlace = header[8:13]
    if depth != 8:
        raise EditError(f"{path}: only 8-bit PNG is supported (got {depth}-bit)",
                        "bad_arguments", "Re-export the PNG as 8-bit.")
    if ctype not in _CHANNELS:
        raise EditError(f"{path}: unsupported PNG colour type {ctype}", "bad_arguments")
    if interlace:
        raise EditError(f"{path}: interlaced PNG is not supported", "bad_arguments")
    channels = _CHANNELS[ctype]
    raw = zlib.decompress(bytes(idat))
    return {"width": w, "height": h, "channels": channels, "pixels": _unfilter(raw, w, h, channels)}


def image_size(path: Path) -> tuple:
    """Width/height of a PNG or JPEG without external libraries."""
    p = Path(path)
    with open(p, "rb") as fh:
        head = fh.read(32)
    if head[:8] == PNG_SIG:
        w, h = struct.unpack(">II", head[16:24])
        return int(w), int(h)
    if head[:2] == b"\xff\xd8":
        with open(p, "rb") as fh:
            data = fh.read()
        i = 2
        n = len(data)
        while i < n - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
                i += 2
                continue
            (seglen,) = struct.unpack(">H", data[i + 2:i + 4])
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return int(w), int(h)
            i += 2 + seglen
    raise EditError(f"cannot determine the size of {p.name}", "bad_arguments",
                    "Supported inputs: PNG, JPEG. Convert other formats first.")


# ---------------------------------------------------------------------------
# Model pool
# ---------------------------------------------------------------------------

def family_of(name: str, priority: list | None = None) -> dict:
    """Classify a model file name into a family/rank, honouring a custom order."""
    raw = Path(str(name)).name
    bare = raw
    for ext in MODEL_EXTS:
        if bare.lower().endswith(ext):
            bare = bare[:-len(ext)]
            break
    low = bare.lower()
    hits = [f for f in FAMILIES if re.search(f[4], low)]
    if hits:
        # A priority list reorders *candidates*; it can never change which family a
        # file belongs to, so every match is ranked and the best one wins.
        keys = list(priority or DEFAULT_EDIT_PRIORITY)

        def weight(entry):
            key, _fam, rank, _can, _pat = entry
            return (keys.index(key) if key in keys else len(keys) + rank, rank)

        key, fam, rank, can_region, _pattern = min(hits, key=weight)
        return {"key": key, "family": fam, "rank": rank,
                "can_region_edit": can_region, "name": raw, "base": bare}
    return {"key": "unknown", "family": "unknown", "rank": 500,
            "can_region_edit": False, "name": raw, "base": bare}


def _scan_dirs(cfg: dict) -> list:
    dirs = []
    conf = cfg.get("edit", {}) if isinstance(cfg, dict) else {}
    for d in (conf.get("models_dirs") or []):
        if d:
            dirs.append(Path(os.path.expandvars(str(d))))
    for env in ("LOCMODEL_MODELS_DIR", "LOCMODEL_MODELS"):
        val = os.environ.get(env)
        if val:
            dirs.extend(Path(p) for p in val.replace(";", os.pathsep).split(os.pathsep) if p)
    return dirs


def _scan_files(root: Path, depth: int = 3, limit: int = 400) -> list:
    found = []
    if not root or not root.is_dir():
        return found
    stack = [(root, 0)]
    while stack and len(found) < limit:
        cur, d = stack.pop()
        try:
            entries = list(cur.iterdir())
        except Exception:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if d < depth:
                        stack.append((entry, d + 1))
                elif entry.suffix.lower() in MODEL_EXTS:
                    found.append(entry)
            except Exception:
                continue
    return found


def _engine_models_raw(cfg: dict, base: str | None = None) -> list:
    """Model names the engine itself reports (authoritative for switching)."""
    try:
        if base is None:
            base = core.base_url(cfg=cfg)[0]
        path = core.cfg_get(cfg, "engine.models_path", "/sdapi/v1/sd-models")
        code, obj = core.http_json(base + path, timeout=10)
    except Exception as exc:
        log(f"engine model list unavailable: {exc}")
        return []
    names = []
    if code == 200 and isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                for key in ("model_name", "title", "filename", "name", "model"):
                    val = item.get(key)
                    if val:
                        names.append(str(val))
                        break
            elif isinstance(item, str):
                names.append(item)
    out, seen = [], set()
    for n in names:
        key = Path(n).name.lower()
        if key not in seen:
            seen.add(key)
            out.append(n)
    return out


def _loaded_model_raw(cfg: dict, base: str | None = None) -> str | None:
    try:
        return core.current_model(base or core.base_url(cfg=cfg)[0], cfg)
    except Exception as exc:
        log(f"cannot read the loaded model: {exc}")
        return None


# Probing a closed engine costs one TCP connect per candidate port, which makes a
# naive call take seconds. Both values are cached for a short while because the
# engine can appear at any moment (the user just has to load a model).
_ENGINE_CACHE = {"key": None, "stamp": 0.0, "models": [], "loaded": None}


def _engine_ttl() -> float:
    try:
        return float(core.cfg_get(_cfg(None), "edit.engine_cache_ttl", 20))
    except Exception:
        return 20.0


def _cache_key(cfg: dict) -> str:
    conf = cfg.get("engine", {}) if isinstance(cfg, dict) else {}
    return json.dumps([str(core.active_config_path()), conf.get("port"),
                       os.environ.get("LOCMODEL_SD_PORT", "")])


def _snapshot_ttl() -> float:
    """Disk cache lifetime. CLI calls are separate processes, so an in-memory cache
    alone buys nothing there; discovery is expensive while no engine is running."""
    try:
        return float(core.cfg_get(_cfg(None), "edit.snapshot_cache_ttl", 120))
    except (TypeError, ValueError):
        return 120.0


def _snapshot_file() -> Path:
    try:
        return Path(core.active_config_path()).parent / "edit_snapshot.json"
    except Exception:
        return Path.home() / ".locmodel-image" / "edit_snapshot.json"


def _snapshot_load(cfg: dict, key: str):
    if _snapshot_ttl() <= 0:
        return None
    try:
        obj = json.loads(_snapshot_file().read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("key") != key:
        return None
    try:
        stamp = float(obj.get("stamp", 0))
    except (TypeError, ValueError):
        return None
    if time.time() - stamp > _snapshot_ttl():
        return None
    models = obj.get("models")
    if not isinstance(models, list):
        return None
    return {"key": key, "stamp": stamp,
            "models": [str(m) for m in models],
            "loaded": obj.get("loaded") or None}


def _snapshot_save(snap: dict) -> None:
    if _snapshot_ttl() <= 0:
        return
    try:
        path = _snapshot_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"key": snap["key"], "stamp": snap["stamp"],
                                    "models": snap["models"],
                                    "loaded": snap["loaded"]}), encoding="utf-8")
    except Exception as exc:
        log(f"cannot persist the model snapshot: {exc}")


def _engine_snapshot(cfg: dict, force: bool = False) -> dict:
    key = _cache_key(cfg)
    if not force:
        if (_ENGINE_CACHE["key"] == key
                and time.time() - _ENGINE_CACHE["stamp"] < _engine_ttl()):
            return _ENGINE_CACHE
        cached = _snapshot_load(cfg, key)
        if cached is not None:
            _ENGINE_CACHE.update(cached)
            return _ENGINE_CACHE
    # Discover once per snapshot: base_url() probes ports when nothing answers, and
    # doing that twice (models + loaded model) is what made a cold call slow.
    try:
        base = core.base_url(cfg=cfg)[0]
    except Exception as exc:
        log(f"engine not reachable: {exc}")
        base = None
    models = _engine_models_raw(cfg, base) if base else []
    loaded = _loaded_model_raw(cfg, base) if base else None
    _ENGINE_CACHE.update(key=key, stamp=time.time(), models=models, loaded=loaded)
    _snapshot_save(_ENGINE_CACHE)
    return _ENGINE_CACHE


def forget_snapshot() -> None:
    """Invalidate both caches after a load/switch, without touching the disk file."""
    _ENGINE_CACHE.update(key=None, stamp=0.0, models=[], loaded=None)
    try:
        path = _snapshot_file()
        if path.exists():
            path.write_text("{}", encoding="utf-8")
    except Exception:
        pass


def _engine_models(cfg: dict) -> list:
    return list(_engine_snapshot(cfg)["models"])


def _loaded_model(cfg: dict):
    return _engine_snapshot(cfg)["loaded"]


def list_models(cfg: dict | None = None, refresh: bool = False) -> dict:
    """Merge engine-reported, on-disk and loaded model information."""
    cfg = _cfg(cfg)
    priority = cfg.get("edit", {}).get("priority") or DEFAULT_EDIT_PRIORITY
    if refresh:
        forget_snapshot()
    engine_names = _engine_models(cfg)
    loaded = _loaded_model(cfg)

    seen, rows = set(), []

    def push(name: str, source: str, disk_path: str | None = None):
        key = Path(name).name.lower()
        for ext in MODEL_EXTS:
            if key.endswith(ext):
                key = key[:-len(ext)]
                break
        if key in seen:
            for row in rows:
                if row["_key"] == key and source not in row["sources"]:
                    row["sources"].append(source)
                    if disk_path and not row.get("path"):
                        row["path"] = disk_path
            return
        seen.add(key)
        fam = family_of(name, priority)
        rows.append({"_key": key, "name": fam["name"], "base": fam["base"],
                     "family": fam["family"], "family_key": fam["key"],
                     "rank": fam["rank"], "can_region_edit": fam["can_region_edit"],
                     "sources": [source], "path": disk_path})

    for n in engine_names:
        push(n, "engine")
    scanned = 0
    for root in _scan_dirs(cfg):
        for f in _scan_files(root):
            push(f.name, "disk", str(f))
            scanned += 1
    if loaded:
        push(loaded, "loaded")

    def is_loaded(row) -> bool:
        if not loaded:
            return False
        a, b = row["_key"], Path(loaded).name.lower()
        for ext in MODEL_EXTS:
            if b.endswith(ext):
                b = b[:-len(ext)]
                break
        return a == b or a.startswith(b) or b.startswith(a)

    for row in rows:
        row["loaded"] = is_loaded(row)
    rows.sort(key=lambda r: (0 if r["loaded"] else 1, r["rank"], r["name"].lower()))

    editable = [r for r in rows if r["can_region_edit"]]
    redrawable = [r for r in rows if r["family"] in ("edit", "edit-ref")]
    prefer_loaded = bool(cfg.get("edit", {}).get("prefer_loaded", True))
    auto_region = _pick(editable, prefer_loaded)
    auto_redraw = _pick(redrawable, prefer_loaded)

    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    return {"ok": True, "loaded": loaded, "models": clean,
            "counts": {"total": len(clean), "region_editable": len(editable),
                       "redrawable": len(redrawable),
                       "scanned_on_disk": scanned},
            "auto": {
                "region_edit": auto_region["name"] if auto_region else None,
                "redraw": auto_redraw["name"] if auto_redraw else None,
                "priority": priority,
                "prefer_loaded": prefer_loaded,
            },
            "notes": _model_notes(editable, clean)}


def _model_notes(editable: list, rows: list) -> list:
    notes = []
    if not rows:
        notes.append("engines reports no models and no model folder is configured; "
                     "set edit.models_dirs to your LocModel model directory")
    if not editable:
        notes.append("no region-editable model (Qwen-Image-Edit / FLUX.2 klein) is installed; "
                     "download one from the LocModel model menu to use edit_image")
    return notes


def _pick(rows: list, prefer_loaded: bool):
    if not rows:
        return None
    if prefer_loaded:
        for row in rows:
            if row.get("loaded"):
                return row
    return sorted(rows, key=lambda r: (r["rank"], 0 if r.get("loaded") else 1,
                                       r["name"].lower()))[0]


def resolve_model(cfg: dict, requested: str | None, purpose: str = "region") -> dict:
    """Return {row, selection} -- never substitutes silently."""
    cfg = _cfg(cfg)
    pool = list_models(cfg)
    rows = pool["models"]
    loaded = pool["loaded"]
    editable = [r for r in rows if r["can_region_edit"]] if purpose == "region" \
        else [r for r in rows if r["family"] in ("edit", "edit-ref")]

    if requested:
        want = Path(requested).name.lower()
        want_bare = want
        for ext in MODEL_EXTS:
            if want_bare.endswith(ext):
                want_bare = want_bare[:-len(ext)]
                break
        hits = []
        for row in rows:
            base = row["base"].lower()
            if want == row["name"].lower() or want_bare == base:
                hits = [row]
                break
            if want_bare in base or base in want_bare:
                hits.append(row)
        if not hits:
            raise EditError(f'model "{requested}" is not available on this machine',
                            "model_not_found",
                            _hint_candidates(rows, purpose))
        chosen = hits[0]
        if purpose == "region" and not chosen["can_region_edit"]:
            raise EditError(
                f'{chosen["name"]} cannot do region editing (family: {chosen["family"]})',
                "model_unsupported",
                "Region editing needs an instruction-edit model (Qwen-Image-Edit, "
                "FLUX.2 klein). Use redraw_image for whole-image restyle, or pick one of: "
                + ", ".join(r["name"] for r in editable[:4]) if editable else
                "No region-editable model is installed.")
        reason = "requested: matched " + ("exactly" if len(hits) == 1 else f"{len(hits)} candidates")
        if len(hits) > 1:
            reason += "; first match used (" + chosen["name"] + ")"
        return {"row": chosen, "loaded": chosen.get("loaded", False),
                "selection": _selection(requested, chosen, rows, reason, editable)}

    chosen = _pick(editable, bool(cfg.get("edit", {}).get("prefer_loaded", True)))
    if not chosen:
        raise EditError(
            "no model on this machine can do region editing",
            "model_unavailable",
            "Install Qwen-Image-Edit (preferred) or FLUX.2 klein from the LocModel model "
            "menu; no configuration change is needed afterwards.")
    if chosen.get("loaded"):
        reason = f"auto: rank-{chosen['rank']} family {chosen['family_key']} already loaded"
    else:
        reason = (f"auto: rank-{chosen['rank']} family {chosen['family_key']} "
                  f"(highest priority available), not loaded -> load on demand")
    return {"row": chosen, "loaded": chosen.get("loaded", False),
            "selection": _selection(None, chosen, rows, reason, editable)}


def _hint_candidates(rows: list, purpose: str) -> str:
    pool = [r["name"] for r in rows
            if (r["can_region_edit"] if purpose == "region" else r["family"] != "unknown")]
    if not pool:
        return ("No editable model was found. Run list_edit_models to see what the engine "
                "reports, and set edit.models_dirs if your models live outside LocModel.")
    head = ", ".join(pool[:6])
    return f"Available candidates: {head}. Names are matched case-insensitively; " \
           f"a unique prefix is enough."


def _selection(requested, chosen, rows, reason, eligible) -> dict:
    cand = []
    for row in rows:
        if row["family"] == "unknown":
            continue
        cand.append({"name": row["name"], "family": row["family"], "rank": row["rank"],
                     "loaded": row.get("loaded", False),
                     "can_region_edit": row["can_region_edit"]})
    cand.sort(key=lambda c: (c["rank"], 0 if c["loaded"] else 1, c["name"].lower()))
    return {"requested": requested, "chosen": chosen["name"], "family": chosen["family"],
            "rank": chosen["rank"], "reason": reason, "candidates": cand[:12],
            "eligible_count": len(eligible)}


# ---------------------------------------------------------------------------
# Mask construction
# ---------------------------------------------------------------------------

def _norm_regions(spec: dict) -> list:
    """Accept {region:..}, {regions:[..]}, {boxes:[[x1,y1,x2,y2],..]}."""
    if not isinstance(spec, dict):
        raise EditError("regions must be an object", "bad_arguments")
    out = []
    if isinstance(spec.get("region"), dict):
        out.append(spec["region"])
    for item in (spec.get("regions") or []):
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, (list, tuple)) and len(item) == 4:
            out.append({"type": "rect", "x": list(item)})
    for box in (spec.get("boxes") or []):
        if isinstance(box, (list, tuple)) and len(box) == 4:
            out.append({"type": "rect", "x": list(box)})
        elif isinstance(box, dict):
            out.append(dict(box, type=box.get("type") or "rect"))
    return out


def _rect_of(region: dict, unit: str, w: int, h: int) -> tuple:
    coords = region.get("x") or region.get("box")
    if coords is None:
        coords = [region.get("x1"), region.get("y1"), region.get("x2"), region.get("y2")]
    if len(coords) != 4 or any(c is None for c in coords):
        raise EditError(f"rect region needs 4 coordinates, got {coords}", "bad_arguments")
    x1, y1, x2, y2 = (float(c) for c in coords)
    if unit == "norm":
        x1, x2 = x1 * w, x2 * w
        y1, y2 = y1 * h, y2 * h
    x1, x2 = sorted((int(round(x1)), int(round(x2))))
    y1, y2 = sorted((int(round(y1)), int(round(y2))))
    return (max(0, x1), max(0, y1), min(w, x2), min(h, y2))


def _fill_rect(buf: bytearray, w: int, h: int, x1: int, y1: int, x2: int, y2: int, val: int):
    if x2 <= x1 or y2 <= y1:
        return
    row = bytes([val]) * (x2 - x1)
    for y in range(y1, y2):
        off = y * w
        buf[off + x1:off + x2] = row


def _fill_polygon(buf: bytearray, w: int, h: int, pts: list, val: int):
    """Scanline fill; coordinates may be float."""
    if len(pts) < 3:
        raise EditError("polygon needs at least 3 points", "bad_arguments")
    ys = [p[1] for p in pts]
    y_start = max(0, int(min(ys)))
    y_end = min(h - 1, int(max(ys)) + 1)
    n = len(pts)
    for y in range(y_start, y_end + 1):
        yc = y + 0.5
        xs = []
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            if (y1 <= yc < y2) or (y2 <= yc < y1):
                xs.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
        if not xs:
            continue
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            a = max(0, int(round(xs[i])))
            b = min(w, int(round(xs[i + 1])))
            if b > a:
                off = y * w
                buf[off + a:off + b] = bytes([val]) * (b - a)


def _fill_disc(buf: bytearray, w: int, h: int, cx: float, cy: float, r: float, val: int):
    if r <= 0:
        return
    y0 = max(0, int(cy - r))
    y1 = min(h - 1, int(cy + r))
    rr = r * r
    for y in range(y0, y1 + 1):
        dy = y + 0.5 - cy
        if abs(dy) > r:
            continue
        span = (rr - dy * dy) ** 0.5
        a = max(0, int(round(cx - span)))
        b = min(w, int(round(cx + span)))
        if b > a:
            off = y * w
            buf[off + a:off + b] = bytes([val]) * (b - a)


def _points_of(region: dict, unit: str, w: int, h: int) -> list:
    pts = region.get("points") or []
    out = []
    for p in pts:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        x, y = float(p[0]), float(p[1])
        if unit == "norm":
            x, y = x * w, y * h
        out.append((x, y))
    return out


def _color_region(buf: bytearray, w: int, h: int, region: dict, src: Path, val: int) -> int:
    """Paint pixels of ``src`` whose colour is within tolerance of rgb."""
    rgb = region.get("rgb") or region.get("color") or []
    if isinstance(rgb, str):
        rgb = [int(v) for v in re.findall(r"\d+", rgb)[:3]]
    if len(rgb) != 3:
        raise EditError("color region needs rgb: [r, g, b]", "bad_arguments")
    tol = int(region.get("tolerance", 32))
    img = read_png(src)
    if img["channels"] not in (3, 4):
        raise EditError("colour matching needs an RGB/RGBA PNG", "bad_arguments",
                        "Convert the source image to RGB PNG first.")
    if (img["width"], img["height"]) != (w, h):
        raise EditError("source size mismatch while colour matching", "bad_arguments")
    px = img["pixels"]
    ch = img["channels"]
    tr, tg, tb = rgb
    hit = 0
    for i in range(0, w * h):
        o = i * ch
        if (abs(px[o] - tr) <= tol and abs(px[o + 1] - tg) <= tol
                and abs(px[o + 2] - tb) <= tol):
            buf[i] = val
            hit += 1
    return hit


def build_mask(path: Path, width: int, height: int, spec: dict,
               source: Path | None = None) -> dict:
    """Render a greyscale PNG mask from region descriptions."""
    unit = (spec.get("unit") or "norm").lower()
    if unit not in ("norm", "px"):
        raise EditError(f'unit must be "norm" or "px", got {unit!r}', "bad_arguments")
    regions = _norm_regions(spec)
    if not regions:
        raise EditError("no regions given", "bad_arguments",
                        "Pass region/regions/boxes, or pass an existing mask_image.")
    buf = bytearray(width * height)
    drawn = []
    for region in regions:
        kind = (region.get("type") or "rect").lower()
        if kind in ("rect", "rectangle", "box"):
            x1, y1, x2, y2 = _rect_of(region, unit, width, height)
            _fill_rect(buf, width, height, x1, y1, x2, y2, 255)
            drawn.append({"type": "rect", "px": [x1, y1, x2, y2]})
        elif kind == "polygon":
            pts = _points_of(region, unit, width, height)
            _fill_polygon(buf, width, height, pts, 255)
            drawn.append({"type": "polygon", "points": len(pts)})
        elif kind in ("brush", "stroke", "circle", "disc"):
            pts = _points_of(region, unit, width, height)
            if kind == "circle":
                cx, cy = pts[0] if pts else (width / 2, height / 2)
                r = float(region.get("radius", 0.1))
                r = r * min(width, height) if unit == "norm" else r
                _fill_disc(buf, width, height, cx, cy, r, 255)
            else:
                radius = float(region.get("radius", 0.02))
                radius = radius * min(width, height) if unit == "norm" else radius
                for (cx, cy) in pts:
                    _fill_disc(buf, width, height, cx, cy, radius, 255)
            drawn.append({"type": kind, "points": len(pts)})
        elif kind in ("color", "colour"):
            if source is None:
                raise EditError("a colour region needs the source image", "bad_arguments")
            hit = _color_region(buf, width, height, region, source, 255)
            drawn.append({"type": "color", "pixels": hit})
        else:
            raise EditError(f"unsupported region type {kind!r}", "bad_arguments",
                            "Supported: rect, polygon, brush, circle, color.")
    _validate_mask(buf, width, height)
    if spec.get("invert"):
        inv = bytearray(width * height)
        for i, v in enumerate(buf):
            inv[i] = 255 - v
        buf = inv
    write_png_gray(path, width, height, bytes(buf))
    return {"path": str(path), "regions": drawn, "coverage": _coverage(buf)}


def _coverage(buf) -> float:
    white = sum(1 for v in buf if v > 127) if len(buf) < 4_000_000 else \
        sum(buf.count(v) for v in range(128, 256))
    return round(white / max(1, len(buf)), 4)


def _validate_mask(buf, width: int, height: int) -> None:
    white = buf.count(255)
    if white == 0:
        raise EditError("mask is empty: nothing would be redrawn", "bad_arguments",
                        "Check the coordinates and the unit (norm = 0..1, px = pixels).")
    ratio = white / max(1, width * height)
    if ratio > 0.98:
        raise EditError("mask covers the whole image; use redraw_image instead",
                        "bad_arguments",
                        "A mask above 98% is a full repaint -- call redraw_image for that.")


def load_mask(path: Path, width: int, height: int) -> dict:
    """Validate a user-supplied mask PNG and rescale it if needed."""
    head = png_header(Path(path))
    if (head["width"], head["height"]) == (width, height):
        img = read_png(Path(path))
        buf = _to_gray(img)
        _validate_mask(buf, width, height)
        return {"path": str(path), "rescaled": False, "coverage": _coverage(buf)}
    img = read_png(Path(path))
    gray = _to_gray(img)
    resized = _resize_gray(gray, img["width"], img["height"], width, height)
    out = Path(path)
    write_png_gray(out, width, height, bytes(resized))
    _validate_mask(resized, width, height)
    return {"path": str(out), "rescaled": True,
            "from": [img["width"], img["height"]], "coverage": _coverage(resized)}


def _to_gray(img: dict) -> bytearray:
    px = img["pixels"]
    ch = img["channels"]
    n = img["width"] * img["height"]
    if ch == 1:
        return bytearray(px)
    out = bytearray(n)
    if ch == 2:
        for i in range(n):
            out[i] = px[i * 2]
    else:
        for i in range(n):
            o = i * ch
            out[i] = (px[o] * 299 + px[o + 1] * 587 + px[o + 2] * 114) // 1000
    return out


def _resize_gray(src, sw: int, sh: int, dw: int, dh: int) -> bytearray:
    out = bytearray(dw * dh)
    if (sw, sh) == (dw, dh):
        return bytearray(src)
    xmap = [min(sw - 1, int(x * sw / dw)) for x in range(dw)]
    for y in range(dh):
        sy = min(sh - 1, int(y * sh / dh))
        base = sy * sw
        o = y * dw
        for x in range(dw):
            out[o + x] = src[base + xmap[x]]
    return out


def _resize_rgb(src, sw: int, sh: int, dw: int, dh: int, ch: int) -> bytearray:
    """Bilinear resize for RGB/RGBA buffers (used only for optional crop upscale)."""
    out = bytearray(dw * dh * ch)
    if (sw, sh) == (dw, dh):
        return bytearray(src)
    xr = [(x + 0.5) * sw / dw - 0.5 for x in range(dw)]
    yr = [(y + 0.5) * sh / dh - 0.5 for y in range(dh)]
    x0 = [max(0, min(sw - 1, int(v))) for v in xr]
    y0 = [max(0, min(sh - 1, int(v))) for v in yr]
    x1 = [min(sw - 1, v + 1) for v in x0]
    y1 = [min(sh - 1, v + 1) for v in y0]
    xw = [min(1.0, max(0.0, v - int(v))) for v in xr]
    yw = [min(1.0, max(0.0, v - int(v))) for v in yr]
    for y in range(dh):
        wy, ay, by = yw[y], y0[y] * sw, y1[y] * sw
        for x in range(dw):
            wx, a1, b1 = xw[x], x0[x], x1[x]
            o = (y * dw + x) * ch
            p00 = (ay + a1) * ch
            p01 = (ay + b1) * ch
            p10 = (by + a1) * ch
            p11 = (by + b1) * ch
            w00 = (1 - wx) * (1 - wy)
            w01 = wx * (1 - wy)
            w10 = (1 - wx) * wy
            w11 = wx * wy
            for c in range(ch):
                out[o + c] = int(src[p00 + c] * w00 + src[p01 + c] * w01
                                 + src[p10 + c] * w10 + src[p11 + c] * w11 + 0.5)
    return out


def write_png_rgb(path: Path, width: int, height: int, pixels: bytes, channels: int = 3) -> Path:
    ctype = 2 if channels == 3 else 6
    raw = bytearray()
    stride = width * channels
    for y in range(height):
        raw.append(0)
        raw += pixels[y * stride:(y + 1) * stride]
    blob = (PNG_SIG
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, ctype, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


def recompose(edited: bytes, original: Path, mask_path: Path, out_path: Path) -> dict:
    """result = where(mask > 127) edited else original  -- enforced on the CPU."""
    tmp = out_path.with_suffix(".engine.png")
    tmp.write_bytes(edited)
    if tmp.read_bytes()[:8] != PNG_SIG:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "reason": "engine returned a non-PNG image; CPU recompose skipped "
                                       "(outside-mask pixels are as the engine produced them)"}
    ed = read_png(tmp)
    mask = read_png(mask_path)
    tw, th = ed["width"], ed["height"]
    mg = _to_gray(mask)
    if (mask["width"], mask["height"]) != (tw, th):
        mg = _resize_gray(mg, mask["width"], mask["height"], tw, th)
    need_rgb = ed["channels"] >= 3
    orig = read_png(original) if original.suffix.lower() == ".png" else None
    if orig is None:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "reason": "original is not a PNG; CPU recompose skipped"}
    if orig["channels"] != ed["channels"] or (orig["width"], orig["height"]) != (tw, th):
        # Rescale the original to the edited geometry (nearest neighbour keeps it honest)
        og = orig["pixels"]
        if orig["channels"] == ed["channels"]:
            if need_rgb:
                og = _resize_rgb(og, orig["width"], orig["height"], tw, th, ed["channels"])
            else:
                og = _resize_gray(og, orig["width"], orig["height"], tw, th)
        orig = {"channels": ed["channels"], "pixels": og, "width": tw, "height": th}
    ep = ed["pixels"]
    op = orig["pixels"]
    ch = ed["channels"]
    out = bytearray(ep)
    changed = 0
    for i in range(tw * th):
        if mg[i] > 127:
            changed += 1
            continue
        o = i * ch
        out[o:o + ch] = op[o:o + ch]
    write_png_rgb(out_path, tw, th, bytes(out), channels=ch)
    tmp.unlink(missing_ok=True)
    return {"ok": True, "path": str(out_path), "protected_pixels": tw * th - changed,
            "redrawn_pixels": changed}


# ---------------------------------------------------------------------------
# Engine calls
# ---------------------------------------------------------------------------

def _data_uri(path: Path) -> str:
    data = Path(path).read_bytes()
    mime = "image/png" if data[:8] == PNG_SIG else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def _out_dir(cfg: dict, out_dir: str | None) -> Path:
    return Path(out_dir or core.cfg_get(cfg, "output.dir") or (Path.cwd() / "output"))


def _stamp_name(prefix: str, tag: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w\-]+", "_", prefix or "locmodel")[:40] or "locmodel"
    return f"{safe}_{tag}_{stamp}"


def _poll_job(cfg: dict, base: str, job_id: str, timeout: int, poll: float) -> dict:
    jobs_path = core.cfg_get(cfg, "engine.jobs_path", "/sdcpp/v1/jobs")
    deadline = time.time() + timeout
    last_err = None
    polls = 0
    state = None
    while time.time() < deadline:
        time.sleep(poll)
        polls += 1
        try:
            sc, st = core.http_json(f"{base}{jobs_path}/{job_id}", timeout=30)
        except Exception as exc:  # noqa: BLE001 - a slow poll must not abandon a job
            last_err = f"{type(exc).__name__}: {exc}"
            if polls % 10 == 0:
                log(f"poll #{polls} for {job_id} failed: {last_err}")
            continue
        if isinstance(st, dict):
            state = st
            if st.get("status") in ("succeeded", "completed", "failed", "cancelled"):
                break
    if state is None and job_id:
        try:
            core.http_json(f"{base}{jobs_path}/{job_id}/cancel", payload={}, timeout=15)
        except Exception as exc:  # noqa: BLE001 - best effort
            log(f"cancel of {job_id} failed: {exc}")
    return {"state": state or {}, "polls": polls, "last_error": last_err}


def _payload_img_gen(kw: dict) -> dict:
    payload = {
        "prompt": kw["prompt"],
        "negative_prompt": kw.get("negative_prompt") or "",
        "width": kw["width"],
        "height": kw["height"],
        "seed": kw["seed"],
        "init_image": _data_uri(kw["init_image"]),
        "sample_params": {"sample_steps": kw["steps"], "sample_method": kw["sampler"]},
    }
    if kw.get("strength") is not None:
        payload["strength"] = float(kw["strength"])
    if kw.get("cfg_scale") is not None:
        payload["sample_params"]["guidance"] = {"txt_cfg": float(kw["cfg_scale"])}
    if kw.get("mask_path"):
        payload["mask_image"] = _data_uri(kw["mask_path"])
    if kw.get("model"):
        payload["model"] = kw["model"]
    return payload


def _payload_img2img(kw: dict) -> dict:
    payload = {
        "init_images": [_data_uri(kw["init_image"])],
        "prompt": kw["prompt"],
        "negative_prompt": kw.get("negative_prompt") or "",
        "width": kw["width"],
        "height": kw["height"],
        "seed": kw["seed"],
        "steps": kw["steps"],
        "sampler_name": kw["sampler"],
        "denoising_strength": float(kw.get("strength") or 0.75),
        "batch_size": 1,
        "n_iter": 1,
    }
    if kw.get("cfg_scale") is not None:
        payload["cfg_scale"] = float(kw["cfg_scale"])
    if kw.get("mask_path"):
        payload["mask"] = _data_uri(kw["mask_path"])
        payload["inpainting_mask_invert"] = 0
        payload["inpaint_full_res"] = 1
        if kw.get("mask_blur") is not None:
            payload["mask_blur"] = int(kw["mask_blur"])
        if kw.get("inpaint_padding") is not None:
            payload["inpaint_full_res_padding"] = int(kw["inpaint_padding"])
    if kw.get("model"):
        payload["override_settings"] = {"sd_model_checkpoint": kw["model"]}
    return payload


def _run_edit(cfg: dict, kw: dict, channel: str, timeout: int, retry: int) -> dict:
    """Submit one image-to-image request through the chosen channel."""
    base, port, src = core.base_url(cfg=cfg)
    poll = float(core.cfg_get(cfg, "generate.poll_interval", 2.0))
    attempts = []
    order = ["img_gen", "img2img"] if channel in ("auto", None) else [channel]
    for name in order:
        payload = _payload_img_gen(kw) if name == "img_gen" else _payload_img2img(kw)
        if name == "img_gen":
            path = "/sdcpp/v1/img_gen"
            code, obj = core.http_json(base + path, payload=payload, timeout=90)
            if code not in (200, 201, 202) or not isinstance(obj, dict):
                attempts.append({"channel": name, "stage": "submit", "http": code,
                                 "error": str(obj)[:300]})
                if channel != "auto":
                    break
                continue
            job_id = obj.get("id")
            polled = _poll_job(cfg, base, job_id, timeout, poll)
            state = polled["state"]
            status = state.get("status")
            if status in ("succeeded", "completed"):
                images = ((state.get("result") or {}).get("images") or [])
                if images:
                    attempts.append({"channel": name, "status": status, "images": len(images)})
                    return {"ok": True, "images": images, "channel": name, "job_id": job_id,
                            "port": port, "detected_by": src, "attempts": attempts,
                            "payload_keys": sorted(payload.keys())}
            err = (state.get("error") or {})
            attempts.append({"channel": name, "status": status,
                             "error": err.get("message") or err.get("code") or status or "timeout",
                             "polls": polled["polls"]})
            if channel != "auto":
                break
        else:
            path = "/sdapi/v1/img2img"
            code, obj = core.http_json(base + path, payload=payload, timeout=timeout)
            ok = code in (200, 201) and isinstance(obj, dict) and obj.get("images")
            attempts.append({"channel": name, "stage": "sync", "http": code,
                             "images": len(obj.get("images") or []) if isinstance(obj, dict) else 0,
                             **({} if ok else {"error": str(obj)[:300]})})
            if ok:
                return {"ok": True, "images": obj["images"], "channel": name, "job_id": None,
                        "port": port, "detected_by": src, "attempts": attempts,
                        "payload_keys": sorted(payload.keys())}
            if channel != "auto":
                break
    raise EditError("the engine rejected the image-to-image request on every channel",
                    "edit_failed",
                    "Attempts: " + json.dumps(attempts, ensure_ascii=False)[:600]
                    + " | Run engine_doctor, and confirm the model can edit images.")


def _save_outputs(cfg: dict, images: list, out_dir: str | None, prefix: str,
                  tag: str, seed: int) -> list:
    folder = _out_dir(cfg, out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    base_name = _stamp_name(prefix, tag)
    max_download = int(core.cfg_get(cfg, "generate.max_download_mb", 64)) * 1024 * 1024
    files = []
    for i, img in enumerate(images):
        raw = None
        if isinstance(img, str):
            img = {"b64_json": img}
        if isinstance(img, dict):
            raw = core._decode_image(img, max_download)
        if not raw:
            continue
        ext = ".png" if raw[:8] == PNG_SIG else ".jpg"
        fp = folder / f"{base_name}_{i + 1}_seed{seed}{ext}"
        n = 2
        while fp.exists():
            fp = folder / f"{base_name}_{i + 1}_seed{seed}_{n}{ext}"
            n += 1
        fp.write_bytes(raw)
        files.append({"path": str(fp.resolve()), "bytes": len(raw)})
    return files


def load_model(cfg: dict, name: str, timeout: int = 240, wait: bool = True) -> dict:
    """Switch the engine to another model, probing the endpoints it may expose."""
    cfg = _cfg(cfg)
    base, port, src = core.base_url(cfg=cfg)
    before = core.current_model(base, cfg)
    candidates = [
        ("/sdcpp/v1/models/load", {"model": name}),
        ("/sdcpp/v1/models/switch", {"model": name}),
        ("/sdapi/v1/options", {"sd_model_checkpoint": name}),
        ("/sdapi/v1/models/load", {"model": name}),
    ]
    remembered = core.cfg_get(cfg, "edit.load_endpoint")
    if remembered:
        picked = [c for c in candidates if c[0] == remembered]
        candidates = picked + [c for c in candidates if c[0] != remembered]
    attempts = []
    for path, payload in candidates:
        try:
            code, obj = core.http_json(base + path, payload=payload, timeout=30)
        except Exception as exc:  # noqa: BLE001
            attempts.append({"endpoint": path, "error": f"{type(exc).__name__}: {exc}"})
            continue
        ok = code in (200, 201, 202) and not (
            isinstance(obj, dict) and (obj.get("error") or obj.get("success") is False))
        attempts.append({"endpoint": path, "http": code,
                         **({} if ok else {"body": str(obj)[:200]})})
        if not ok:
            continue
        try:
            cur = cfg
            cur.setdefault("edit", {})["load_endpoint"] = path
            core.save_config(cur)
        except Exception as exc:  # noqa: BLE001 - remembering is a nicety
            log(f"cannot remember the load endpoint: {exc}")
        forget_snapshot()
        final = None
        if wait:
            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(2.0)
                cur_name = core.current_model(base, cfg)
                if cur_name and (Path(cur_name).stem.lower() == Path(name).stem.lower()
                                 or Path(name).stem.lower() in cur_name.lower()):
                    final = cur_name
                    break
        return {"ok": True, "endpoint": path, "before": before,
                "after": final or core.current_model(base, cfg), "confirmed": bool(final),
                "attempts": attempts, "port": port, "detected_by": src}
    raise EditError(f'the engine did not accept a model switch to "{name}"',
                    "load_failed",
                    "Tried: " + ", ".join(a["endpoint"] for a in attempts)
                    + " | Switch the model in the LocModel window instead.")


def _ensure_model(cfg: dict, sel: dict, purpose: str) -> dict:
    """Load the selected model when needed and allowed."""
    info = {"auto_loaded": False, "load": None}
    if sel.get("loaded"):
        return info
    if not bool(core.cfg_get(cfg, "edit.auto_load", True)):
        raise EditError(
            f'{sel["row"]["name"]} is not the loaded model and edit.auto_load is off',
            "model_not_loaded",
            f'Call load_model with model="{sel["row"]["name"]}", or set edit.auto_load=true.')
    res = load_model(cfg, sel["row"]["name"])
    info["auto_loaded"] = True
    info["load"] = {k: res[k] for k in ("endpoint", "before", "after", "confirmed")}
    if not res.get("confirmed"):
        info["load"]["warning"] = ("the switch was accepted but the engine never reported the "
                                   "new model; the edit may run on the previous model")
    return info


def _eng_defaults(cfg: dict) -> dict:
    """Engine-reported defaults, or {} when the engine is unreachable (dry runs)."""
    try:
        return core.engine_defaults(cfg=cfg) or {}
    except Exception as exc:  # noqa: BLE001 - a missing engine must not block a dry run
        log(f"engine defaults unavailable: {type(exc).__name__}: {exc}")
        return {}


def _prepare(cfg: dict, init_image: str, channel: str, model: str | None,
             purpose: str, allow_missing: bool = False) -> dict:
    src_path = Path(init_image).expanduser()
    if not src_path.is_file():
        raise EditError(f"init_image not found: {src_path}", "bad_arguments")
    width, height = image_size(src_path)
    sel, warning = None, None
    try:
        sel = resolve_model(cfg, model, purpose=purpose)
    except EditError as exc:
        if not allow_missing:
            raise
        warning = f"{exc.code}: {exc}"
    return {"src": src_path, "width": width, "height": height, "sel": sel,
            "warning": warning}


def edit_image(init_image: str, prompt: str, cfg: dict | None = None, *,
               mask_image: str | None = None, regions: dict | None = None,
               strength: float | None = None, negative_prompt: str = "",
               model: str | None = None, steps: int | None = None,
               sampler: str | None = None, cfg_scale: float | None = None,
               seed: int = -1, count: int = 1, mask_blur: int | None = None,
               inpaint_padding: int | None = None, channel: str | None = None,
               out_dir: str | None = None, filename_prefix: str = "locmodel-edit",
               timeout: int | None = None, retry: int | None = None,
               verify: str | None = None, crop_upscale: bool = False,
               dry_run: bool = False) -> dict:
    cfg = _cfg(cfg)
    if not prompt or not prompt.strip():
        raise EditError("prompt must not be empty", "bad_arguments",
                        "Describe the change, e.g. 'repaint this wall in red brick'.")
    channel = channel or core.cfg_get(cfg, "edit.channel", "auto")
    timeout = int(timeout or core.cfg_get(cfg, "edit.timeout", 900))
    src = Path(init_image).expanduser()
    if not src.is_file():
        raise EditError(f"init_image not found: {src}", "bad_arguments")
    width, height = image_size(src)

    mask_info = None
    work = _out_dir(cfg, out_dir)
    work.mkdir(parents=True, exist_ok=True)
    if mask_image:
        mp = Path(mask_image).expanduser()
        if not mp.is_file():
            raise EditError(f"mask_image not found: {mp}", "bad_arguments")
        mask_info = load_mask(mp, width, height)
        mask_path = Path(mask_info["path"])
    elif regions:
        mask_path = work / f"{_stamp_name(filename_prefix, 'mask')}.png"
        mask_info = build_mask(mask_path, width, height, regions, source=src)
    else:
        raise EditError("an edit needs a mask: pass mask_image or regions",
                        "bad_arguments",
                        'Use the annotator (annotator/mask_annotator.html) to draw a mask, '
                        'or pass regions={"regions":[{"type":"rect","x":[0.1,0.2,0.5,0.6]}]}.')

    if crop_upscale:
        raise EditError("crop_upscale is not available in this build", "not_supported",
                        "Enlarge the source image first (e.g. with the LocModel UI), then edit.")

    eng = _eng_defaults(cfg)
    steps = int(steps if steps is not None else eng.get("steps") or 20)
    sampler = sampler or eng.get("sampler") or "euler"
    if cfg_scale is None:
        cfg_scale = eng.get("cfg")
    if seed is None or int(seed) < 0:
        seed = 1234567890 if dry_run else random.randint(1, 2 ** 31 - 1)
    seed = int(seed)
    strength = float(strength if strength is not None else
                     core.cfg_get(cfg, "edit.strength", 0.75))

    sel = None
    sel_warning = None
    if dry_run:
        # A dry run validates the request itself; resolving the model would probe
        # (and possibly start) the engine, which is pointless here.
        sel_warning = "dry run: the model is resolved when the job is submitted"
    else:
        sel = resolve_model(cfg, model, purpose="region")
    kw = {"prompt": prompt.strip(), "negative_prompt": negative_prompt,
          "width": width, "height": height, "steps": steps, "sampler": sampler,
          "cfg_scale": cfg_scale, "seed": seed, "strength": strength,
          "init_image": src, "mask_path": mask_path, "mask_blur": mask_blur,
          "inpaint_padding": inpaint_padding,
          "model": sel["row"]["name"] if sel else (model or "<auto>")}

    if dry_run:
        out = {"ok": True, "dry_run": True, "channel": channel,
               "payload_keys": sorted(_payload_img_gen(kw).keys()),
               "size": [width, height], "strength": strength,
               "mask": mask_info,
               "selection": sel["selection"] if sel else None}
        if sel_warning:
            out["warning"] = sel_warning
        return out

    load_info = _ensure_model(cfg, sel, "region")
    res = _run_edit(cfg, kw, channel, timeout, int(retry or 1))
    files = _save_outputs(cfg, res["images"], out_dir, filename_prefix, "edit", seed)

    # Guarantee: outside the mask nothing changes. The protected bytes replace the
    # file at ``path`` (so the default output path is always the safe one) and the
    # engine's raw output is kept next to it as ``*_raw.png`` for inspection.
    recomposed = []
    for f in files:
        out_path = Path(f["path"])
        raw_path = out_path.with_name(out_path.stem + "_raw" + out_path.suffix)
        try:
            raw_bytes = out_path.read_bytes()
            raw_path.write_bytes(raw_bytes)
            info = recompose(raw_bytes, src, mask_path, out_path)
        except Exception as exc:  # noqa: BLE001
            info = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        if info.get("ok"):
            f["raw"] = str(raw_path)
            f["protected"] = True
            f["bytes"] = Path(info["path"]).stat().st_size
            recomposed.append({"path": info["path"], "raw": str(raw_path),
                               "of": str(raw_path),
                               "protected_pixels": info["protected_pixels"],
                               "redrawn_pixels": info["redrawn_pixels"]})
        else:
            f["protected"] = False
    verify_mode = verify or core.cfg_get(cfg, "edit.verify", "agent")
    result = {
        "ok": True, "files": files, "recomposed": recomposed,
        "params": {"prompt": kw["prompt"], "negative_prompt": negative_prompt,
                   "width": width, "height": height, "steps": steps, "sampler": sampler,
                   "cfg": cfg_scale, "seed": seed, "strength": strength,
                   "mask_blur": mask_blur, "inpaint_padding": inpaint_padding},
        "mask": mask_info, "selection": sel["selection"], "model_load": load_info,
        "channel": res["channel"], "attempts": res["attempts"], "job_id": res["job_id"],
        "engine": {"port": res["port"], "detected_by": res["detected_by"]},
        "size_notes": [], "verify": _verify_block(verify_mode, recomposed or files),
        "elapsed_s": None,
    }
    if not recomposed:
        result["notes"] = ["CPU recompose unavailable (engine output was not PNG); "
                           "outside-mask pixels are whatever the engine produced."]
    if channel == "auto" and res["channel"] != core.cfg_get(cfg, "edit.resolved_channel"):
        try:
            cfg.setdefault("edit", {})["resolved_channel"] = res["channel"]
            core.save_config(cfg)
        except Exception as exc:  # noqa: BLE001
            log(f"cannot remember the channel: {exc}")
    return result


def _verify_block(mode: str, files: list) -> dict:
    mode = (mode or "agent").lower()
    if mode == "off":
        return {"mode": "off"}
    if mode == "local":
        return {"mode": "local", "available": False,
                "how": "no local vision engine is configured in this build; falling back to "
                       "the agent. See docs/CHANGELOG.md for the hook."}
    return {"mode": "agent", "available": True,
            "instruction": "Open the returned image(s) and check: (1) the requested change "
                           "appeared inside the mask, (2) the rest of the picture is untouched "
                           "(seams, duplicated edges, smeared faces), (3) nothing required was "
                           "deleted. If it failed, retry once with a larger strength or a "
                           "tighter mask before changing models.",
            "images": [f["path"] for f in files]}


def redraw_image(init_image: str, prompt: str, cfg: dict | None = None, *,
                 strength: float | None = None, negative_prompt: str = "",
                 model: str | None = None, steps: int | None = None,
                 sampler: str | None = None, cfg_scale: float | None = None,
                 seed: int = -1, count: int = 1, channel: str | None = None,
                 out_dir: str | None = None, filename_prefix: str = "locmodel-redraw",
                 timeout: int | None = None, verify: str | None = None,
                 dry_run: bool = False) -> dict:
    cfg = _cfg(cfg)
    if not prompt or not prompt.strip():
        raise EditError("prompt must not be empty", "bad_arguments")
    channel = channel or core.cfg_get(cfg, "edit.channel", "auto")
    timeout = int(timeout or core.cfg_get(cfg, "edit.timeout", 900))
    if dry_run:
        src_path = Path(init_image).expanduser()
        if not src_path.is_file():
            raise EditError(f"init_image not found: {src_path}", "bad_arguments")
        prep = {"src": src_path, "width": image_size(src_path)[0],
                "height": image_size(src_path)[1],
                "sel": None, "warning": "dry run: the model is resolved when the job is submitted"}
    else:
        prep = _prepare(cfg, init_image, channel, model, "redraw")
    sel = prep["sel"]
    width, height = prep["width"], prep["height"]
    eng = _eng_defaults(cfg)
    steps = int(steps if steps is not None else eng.get("steps") or 20)
    sampler = sampler or eng.get("sampler") or "euler"
    if cfg_scale is None:
        cfg_scale = eng.get("cfg")
    if seed is None or int(seed) < 0:
        seed = 1234567890 if dry_run else random.randint(1, 2 ** 31 - 1)
    strength = float(strength if strength is not None else
                     core.cfg_get(cfg, "edit.redraw_strength", 0.6))
    kw = {"prompt": prompt.strip(), "negative_prompt": negative_prompt,
          "width": width, "height": height, "steps": steps, "sampler": sampler,
          "cfg_scale": cfg_scale, "seed": int(seed), "strength": strength,
          "init_image": prep["src"], "mask_path": None,
          "model": sel["row"]["name"] if sel else (model or "<auto>")}
    if dry_run:
        out = {"ok": True, "dry_run": True, "channel": channel,
               "payload_keys": sorted(_payload_img_gen(kw).keys()),
               "size": [width, height], "strength": strength,
               "selection": sel["selection"] if sel else None}
        if prep.get("warning"):
            out["warning"] = prep["warning"]
        return out
    load_info = _ensure_model(cfg, sel, "redraw")
    res = _run_edit(cfg, kw, channel, timeout, 1)
    files = _save_outputs(cfg, res["images"], out_dir, filename_prefix, "redraw", int(seed))
    verify_mode = verify or core.cfg_get(cfg, "edit.verify", "agent")
    return {"ok": True, "files": files, "recomposed": [],
            "params": {"prompt": kw["prompt"], "negative_prompt": negative_prompt,
                       "width": width, "height": height, "steps": steps, "sampler": sampler,
                       "cfg": cfg_scale, "seed": int(seed), "strength": strength},
            "selection": sel["selection"], "model_load": load_info,
            "channel": res["channel"], "attempts": res["attempts"], "job_id": res["job_id"],
            "engine": {"port": res["port"], "detected_by": res["detected_by"]},
            "verify": _verify_block(verify_mode, files)}
