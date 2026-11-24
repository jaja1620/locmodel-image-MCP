#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Video generation bridge for LocModel's built-in "视频 → 生成视频" panel.

LocModel exposes two *different* ways of making a video and they must never be
confused:

* ``POST /sdcpp/v1/vid_gen``  — the engine-native text/image-to-video endpoint.
  It is served by an sd-server instance whose ``current_mode`` is ``vid_gen``
  and it streams a job (``kind=vid_gen``) that returns one encoded video in
  ``result.b64_json``.  This module talks to that endpoint only.
* ``%LOCALAPPDATA%\\LocModel\\lipsync`` — the InfiniteTalk lip-sync pipeline
  (video 选项卡 → 对口型视频).  It is *not* an HTTP endpoint at all; it is a
  local python runner with its own ~24 GB of weights.  See ``locmodel_lipsync``.

Everything here is stdlib-only so the packaged MCP server keeps working on a
machine without Python dependencies installed.
"""

from __future__ import annotations

import sys

import base64
import json
import mimetypes
import os
import time
import urllib.parse
from pathlib import Path

import locmodel_image as core

LOG = core.LOG

# ---------------------------------------------------------------------------
# Engine contract (measured against LocModel 3.6.1 / engine master-1117-0d71f96)
# ---------------------------------------------------------------------------

SUBMIT_PATH = "/sdcpp/v1/vid_gen"
JOBS_PATH = "/sdcpp/v1/jobs"
RECIPES_PATH = "/sdcpp/v1/recipes"

OUTPUT_FORMATS = ("webm", "webp", "avi")
MEDIA_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
VIDEO_EXT = {".mp4", ".webm", ".avi", ".mov", ".mkv", ".gif", ".webp"}

#: Keys of ``sample_params`` the engine actually reads. Anything else is silently
#: ignored by the engine (it answers 202 whatever we send), so we validate on this
#: side — that is the only defence against silent no-ops.
SAMPLE_KEYS = ("sample_steps", "scheduler", "sample_method", "eta", "flow_shift",
               "custom_sigmas", "shifted_timestep", "guidance", "slg")

#: values that the engine rejects with "invalid generation parameters".
BAD_SCHEDULERS = ("", "default", "auto", "none")

IMAGE_FIELDS = ("init_image", "end_image")
IMAGE_LIST_FIELDS = ("ref_images", "control_frames")


class VideoError(core.BridgeError):
    def __init__(self, message: str, code: str = "video_error", hint: str = "") -> None:
        super().__init__(message, code)
        self.hint = hint


# ---------------------------------------------------------------------------
# Mode / capability checks
# ---------------------------------------------------------------------------

def capabilities(cfg: dict | None = None) -> dict:
    cfg = cfg or core.load_config()
    base, _port, _src = core.base_url(cfg=cfg)
    return core.capabilities(base, cfg)


def engine_mode(cfg: dict | None = None) -> dict:
    """Return what the running engine instance can do.

    A LocModel engine instance is started in exactly one mode. When the GUI is on
    the image tab the same binary answers ``vid_gen`` with an error, and the two
    modes cannot coexist on one port, so we check before submitting.
    """
    caps = capabilities(cfg)
    modes = caps.get("supported_modes") or []
    current = caps.get("current_mode")
    out_formats = (caps.get("output_formats_by_mode") or {}).get("vid_gen") or list(OUTPUT_FORMATS)
    defaults = (caps.get("defaults_by_mode") or {}).get("vid_gen") or {}
    feats = caps.get("features_by_mode", {}).get("vid_gen") if isinstance(
        caps.get("features_by_mode"), dict) else None
    return {
        "ok": True,
        "current_mode": current,
        "supported_modes": modes,
        "video_ready": "vid_gen" in modes or current == "vid_gen",
        "output_formats": list(out_formats),
        "min_width": (caps.get("limits") or {}).get("min_width", 64),
        "max_width": (caps.get("limits") or {}).get("max_width", 4096),
        "min_height": (caps.get("limits") or {}).get("min_height", 64),
        "max_height": (caps.get("limits") or {}).get("max_height", 4096),
        "defaults": defaults,
        "features": feats or caps.get("features") or {},
        "model": (caps.get("model") or {}).get("name"),
    }


def require_video_mode(cfg: dict | None = None) -> dict:
    info = engine_mode(cfg)
    if not info["video_ready"]:
        raise VideoError(
            "the running LocModel engine is not in vid_gen mode (current_mode=%r, "
            "supported=%s). Restart LocModel on the 视频 tab so the engine loads a video "
            "model before calling generate_video." % (info["current_mode"], info["supported_modes"]),
            "engine_not_in_video_mode",
            "LocModel 的引擎实例一次只能跑一种模式：当前是 %s。到「视频」选项卡启动引擎后再试。"
            % (info["current_mode"] or "unknown"))
    return info


# ---------------------------------------------------------------------------
# Media helpers
# ---------------------------------------------------------------------------

def _media_uri(value: str) -> str:
    """Accept a local path, a data URI or raw base64 and return a data URI.

    The engine takes either a data URI or bare base64 for ``init_image`` /
    ``end_image`` (a bare filesystem path is rejected with 400), so paths are
    always converted here.
    """
    text = (value or "").strip()
    if not text:
        raise VideoError("empty image argument", "bad_arguments")
    if text.startswith("data:"):
        return text
    if text.startswith("base64:"):
        return text[len("base64:"):]
    candidate = Path(text).expanduser()
    try:
        is_file = candidate.is_file()
    except OSError:
        is_file = False
    if is_file:
        suffix = candidate.suffix.lower()
        if suffix not in MEDIA_EXT:
            raise VideoError(f"unsupported image type {suffix!r}: {candidate}", "bad_media",
                             "支持 png / jpg / jpeg / webp / bmp / gif")
        mime = mimetypes.guess_type(candidate.name)[0] or "image/png"
        data = candidate.read_bytes()
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    # not a path: assume the caller already handed us base64
    if len(text) > 64 and all(c.isalnum() or c in "+/=\n\r" for c in text[:64]):
        return text
    raise VideoError(f"image not found: {text}", "bad_media",
                     "传本地图片绝对路径，或 data URI / base64 字符串")


def _clean_sample_params(params: dict | None) -> dict:
    out: dict = {}
    for key, val in (params or {}).items():
        if key not in SAMPLE_KEYS:
            raise VideoError(
                f"unsupported sample_params key {key!r}; the engine ignores unknown keys "
                f"silently, so it is rejected here. Allowed: {', '.join(SAMPLE_KEYS)}",
                "bad_arguments")
        if val is None:
            continue
        if key == "scheduler" and str(val).strip().lower() in BAD_SCHEDULERS:
            raise VideoError(
                f"scheduler={val!r} is rejected by the engine (invalid generation parameters). "
                "Use a real scheduler such as 'discrete' / 'karras' / 'exponential'.",
                "bad_arguments")
        if key in ("guidance", "slg") and isinstance(val, dict):
            out[key] = val
            continue
        if key == "custom_sigmas":
            if not isinstance(val, (list, tuple)) or not val:
                raise VideoError("custom_sigmas must be a non-empty numeric array",
                                 "bad_arguments")
            try:
                out[key] = [float(x) for x in val]
            except (TypeError, ValueError):
                raise VideoError("custom_sigmas must be a non-empty numeric array",
                                 "bad_arguments") from None
            continue
        out[key] = val
    return out


def build_payload(prompt: str = "", negative_prompt: str = "", *, width=832, height=480,
                  fps=None, video_frames=None, steps=None, flow_shift=None, cfg_scale=None,
                  scheduler=None, sample_method=None, custom_sigmas=None, seed=None,
                  output_format=None, output_compression=None, init_image=None,
                  end_image=None, ref_images=None, control_frames=None, ref_videos=None,
                  ref_audios=None, ref_video_audios=None, moe_boundary=None,
                  vace_strength=None, high_noise_steps=None, high_noise_boundary=None,
                  vae_tiling: bool | None = None, info: dict | None = None) -> dict:
    """Assemble the ``vid_gen`` body using the engine's own key layout."""
    if not (prompt or "").strip():
        raise VideoError("prompt must not be empty", "bad_arguments",
                         "视频默认从提示词或首帧图开始，二者至少给一个")
    info = info or {}
    lo_w, hi_w = info.get("min_width", 64), info.get("max_width", 4096)
    lo_h, hi_h = info.get("min_height", 64), info.get("max_height", 4096)
    width, height = int(width), int(height)
    if not (lo_w <= width <= hi_w and lo_h <= height <= hi_h):
        raise VideoError(
            f"size {width}x{height} outside the engine limits "
            f"({lo_w}..{hi_w} x {lo_h}..{hi_h})", "bad_arguments")
    if width % 8 or height % 8:
        raise VideoError(f"size {width}x{height} must be a multiple of 8", "bad_arguments",
                         "视频模型的 size_multiple 一般是 8 或 16")

    fmt = (output_format or "webm").lower().lstrip(".")
    allowed = info.get("output_formats") or list(OUTPUT_FORMATS)
    if fmt not in allowed:
        raise VideoError(f"output_format {fmt!r} not supported; engine allows {allowed}",
                         "bad_arguments")

    sample: dict = {}
    if steps is not None:
        sample["sample_steps"] = int(steps)
    if flow_shift is not None:
        sample["flow_shift"] = float(flow_shift)
    if scheduler:
        sample["scheduler"] = str(scheduler)
    if sample_method:
        sample["sample_method"] = str(sample_method)
    if custom_sigmas is not None:
        sample["custom_sigmas"] = custom_sigmas
    if cfg_scale is not None:
        sample["guidance"] = {"txt_cfg": float(cfg_scale)}
    sample = _clean_sample_params(sample)

    payload: dict = {
        "prompt": prompt,
        "negative_prompt": negative_prompt or "",
        "width": width,
        "height": height,
        "output_format": fmt,
    }
    if fps is not None:
        if int(fps) <= 0:
            raise VideoError(f"fps must be positive, got {fps}", "bad_arguments")
        payload["fps"] = int(fps)
    if video_frames is not None:
        if int(video_frames) <= 0:
            raise VideoError(f"video_frames must be positive, got {video_frames}", "bad_arguments")
        payload["video_frames"] = int(video_frames)
    if output_compression is not None:
        payload["output_compression"] = int(output_compression)
    if seed is not None:
        payload["seed"] = int(seed)
    if sample:
        payload["sample_params"] = sample

    # high-noise sampling is model dependent: Wan 2.2 TI2V answers 400 when the
    # two-pass keys are present, so only send them when the caller asked for them.
    hn = {}
    if high_noise_steps is not None:
        hn["sample_steps"] = int(high_noise_steps)
    if high_noise_boundary is not None:
        hn["moe_boundary"] = float(high_noise_boundary)
    if hn:
        payload["high_noise_sample_params"] = hn
    elif moe_boundary is not None:
        payload["moe_boundary"] = float(moe_boundary)

    single = {"init_image": init_image, "end_image": end_image}
    for name in IMAGE_FIELDS:
        val = single[name]
        if val:
            payload[name] = _media_uri(val)
    multi = {"ref_images": ref_images, "control_frames": control_frames}
    for name in IMAGE_LIST_FIELDS:
        val = multi[name]
        if val:
            if isinstance(val, str):
                val = [val]
            payload[name] = [_media_uri(v) for v in val]
    if ref_videos:
        # the engine wants a list of lists here (each inner list = one clip)
        seq = ref_videos if isinstance(ref_videos, (list, tuple)) else [ref_videos]
        payload["ref_videos"] = [[str(v)] if not isinstance(v, (list, tuple)) else list(v)
                                 for v in seq]
    if ref_audios:
        payload["ref_audios"] = list(ref_audios) if isinstance(ref_audios, (list, tuple)) \
            else [ref_audios]
    if ref_video_audios:
        payload["ref_video_audios"] = list(ref_video_audios) \
            if isinstance(ref_video_audios, (list, tuple)) else [ref_video_audios]
    if vace_strength is not None:
        payload["vace_strength"] = float(vace_strength)
    if vae_tiling is not None:
        payload["vae_tiling_params"] = {"enabled": bool(vae_tiling)}
    return payload


# ---------------------------------------------------------------------------
# Submit / poll / cancel
# ---------------------------------------------------------------------------

def submit(payload: dict, cfg: dict | None = None) -> dict:
    cfg = cfg or core.load_config()
    base, port, src = core.base_url(cfg=cfg)
    code, obj = core.http_json(base + SUBMIT_PATH, payload, timeout=120)
    if code is None:
        raise VideoError(f"cannot reach the LocModel engine on port {port}: {obj}",
                         "engine_unreachable", "确认 LocModel 已启动，或调用 engine_discover。")
    if code == 404:
        raise VideoError(
            f"POST {SUBMIT_PATH} -> 404 on port {port}. This engine build does not expose "
            "the video endpoint, or the instance is not a vid_gen instance.",
            "video_endpoint_missing",
            "升级 LocModel / 在「视频」选项卡启动引擎后重试。")
    if code >= 400:
        detail = obj.get("error") if isinstance(obj, dict) else obj
        raise VideoError(f"vid_gen rejected the request ({code}): {detail}",
                         "engine_rejected",
                         "参数非法时引擎返回 invalid generation parameters；"
                         "常见原因：scheduler=default、high_noise_* 给了不支持的模型、"
                         "尺寸不是 size_multiple 的倍数、图片没有包成 data URI。")
    if not isinstance(obj, dict) or not obj.get("id"):
        raise VideoError(f"unexpected submit response: {obj!r}", "bad_response")
    return {"job_id": obj["id"], "poll_url": obj.get("poll_url") or
            f"{JOBS_PATH}/{obj['id']}", "status": obj.get("status"), "port": port,
            "source": src}


def fetch(job_id: str, cfg: dict | None = None) -> dict:
    cfg = cfg or core.load_config()
    base, _port, _src = core.base_url(cfg=cfg)
    code, obj = core.http_json(f"{base}{JOBS_PATH}/{job_id}", timeout=60)
    if code is None:
        raise VideoError(f"cannot reach the engine to poll {job_id}: {obj}",
                         "engine_unreachable")
    if code == 404:
        # the engine keeps finished results only for --completed-ttl-seconds (600 s)
        raise VideoError(f"job {job_id} not found (expired or wrong port)",
                         "job_not_found",
                         "引擎只在 10 分钟内保留结果，超时后需重新生成。")
    if not isinstance(obj, dict):
        raise VideoError(f"unexpected job response: {obj!r}", "bad_response")
    return obj


TERMINAL = ("completed", "failed", "cancelled", "canceled")


def wait(job_id: str, timeout: float = 1800, interval: float = 2.0,
         cfg: dict | None = None) -> dict:
    deadline = time.time() + max(1.0, float(timeout))
    last = {}
    while True:
        last = fetch(job_id, cfg)
        status = str(last.get("status") or "").lower()
        if status in TERMINAL:
            return last
        if time.time() >= deadline:
            last = dict(last)
            last["timed_out"] = True
            return last
        time.sleep(max(0.5, float(interval)))


def cancel(job_id: str, cfg: dict | None = None) -> dict:
    cfg = cfg or core.load_config()
    base, _port, _src = core.base_url(cfg=cfg)
    code, obj = core.http_json(f"{base}{JOBS_PATH}/{job_id}/cancel", {}, timeout=60)
    if code in (404, 405, 410):
        raise VideoError(f"cancel not supported by this engine build ({code})", "cancel_unsupported",
                         "LocModel 3.3.0 返回 410，3.6.1 起支持取消。")
    if code is None or code >= 400:
        raise VideoError(f"cancel failed ({code}): {obj}", "cancel_failed")
    return obj if isinstance(obj, dict) else {"ok": True}


# ---------------------------------------------------------------------------
# Saving results
# ---------------------------------------------------------------------------

def _out_dir(output_dir: str | None, cfg: dict) -> Path:
    if output_dir:
        path = Path(output_dir).expanduser()
    else:
        configured = core.cfg_get(cfg, "video.output_dir", None) or \
            core.cfg_get(cfg, "output_dir", None)
        path = Path(configured).expanduser() if configured else \
            Path.home() / "Videos" / "LocModel"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save(job: dict, output_dir: str | None, prefix: str = "LocModel",
         cfg: dict | None = None) -> list:
    cfg = cfg or {}
    result = job.get("result")
    if not isinstance(result, dict):
        raise VideoError(f"job has no result payload: status={job.get('status')} "
                         f"error={job.get('error')}", "no_result")
    b64 = result.get("b64_json") or result.get("b64") or ""
    if not b64:
        raise VideoError("result carries no b64_json payload", "no_result")
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[-1]
    try:
        data = base64.b64decode(b64, validate=False)
    except Exception as exc:
        raise VideoError(f"cannot decode result: {exc}", "bad_response") from None
    ext = str(result.get("output_format") or "webm").lstrip(".").lower()
    mime = str(result.get("mime_type") or "")
    # trust the bytes over the declared format: webm starts with EBML magic, avi/webp
    # with RIFF. A mismatch would otherwise produce a file that no player opens.
    riff, ebml = data[:4] == b"RIFF", data[:4] == b"\x1aE\xdf\xa3"
    if riff and ext in ("webm",):
        ext = "avi" if data[8:12] == b"AVI " else "webp"
    elif ebml and ext != "webm":
        ext = "webm"
    directory = _out_dir(output_dir, cfg)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = directory / f"{prefix}_{stamp}.{ext}"
    n = 1
    while path.exists():
        path = directory / f"{prefix}_{stamp}_{n}.{ext}"
        n += 1
    path.write_bytes(data)
    LOG.info("saved video %s (%d bytes)", path, len(data))
    return [{"path": str(path), "bytes": len(data), "mime_type": mime,
             "fps": result.get("fps"), "frame_count": result.get("frame_count")}]


# ---------------------------------------------------------------------------
# High level entry points
# ---------------------------------------------------------------------------

def generate(prompt: str, cfg: dict | None = None, *, negative_prompt: str = "",
             width: int = 832, height: int = 480, fps: int | None = None,
             video_frames: int | None = None, steps: int | None = None,
             flow_shift: float | None = None, cfg_scale: float | None = None,
             scheduler: str | None = None, sample_method: str | None = None,
             custom_sigmas: list | None = None, seed: int | None = None,
             output_format: str | None = None, init_image: str | None = None,
             end_image: str | None = None, ref_images=None, control_frames=None,
             ref_videos=None, ref_audios=None, ref_video_audios=None,
             moe_boundary: float | None = None, vace_strength: float | None = None,
             high_noise_steps: int | None = None, high_noise_boundary: float | None = None,
             vae_tiling: bool | None = None, wait_seconds: float = 0,
             timeout: float = 1800, output_dir: str | None = None,
             filename_prefix: str = "LocModel_vid", dry_run: bool = False) -> dict:
    """Submit a vid_gen job and (optionally) wait for it.

    ``wait_seconds=0`` is the default on purpose: a 480P clip takes minutes and
    MCP clients time out, so the caller gets a ``job_id`` back immediately and
    polls with ``job_status``.
    """
    cfg = cfg or core.load_config()
    info = require_video_mode(cfg)
    payload = build_payload(
        prompt, negative_prompt, width=width, height=height, fps=fps,
        video_frames=video_frames, steps=steps, flow_shift=flow_shift,
        cfg_scale=cfg_scale, scheduler=scheduler, sample_method=sample_method,
        custom_sigmas=custom_sigmas, seed=seed, output_format=output_format,
        init_image=init_image, end_image=end_image, ref_images=ref_images,
        control_frames=control_frames, ref_videos=ref_videos, ref_audios=ref_audios,
        ref_video_audios=ref_video_audios, moe_boundary=moe_boundary,
        vace_strength=vace_strength, high_noise_steps=high_noise_steps,
        high_noise_boundary=high_noise_boundary, vae_tiling=vae_tiling, info=info)

    if dry_run:
        return {"ok": True, "dry_run": True, "payload": payload, "mode": info}

    sub = submit(payload, cfg)
    rec = {"ok": True, "job_id": sub["job_id"], "poll_url": sub["poll_url"],
           "status": sub["status"], "payload": payload, "mode": info,
           "files": [], "elapsed_s": 0.0}
    if wait_seconds and float(wait_seconds) > 0:
        t0 = time.time()
        job = wait(sub["job_id"], timeout=min(float(wait_seconds), float(timeout)),
                   interval=core.cfg_get(cfg, "video.poll_interval", 3.0), cfg=cfg)
        rec["elapsed_s"] = round(time.time() - t0, 1)
        rec["status"] = job.get("status")
        rec["job"] = {k: job.get(k) for k in ("status", "error", "progress",
                                              "cancel_requested", "queue_position")}
        if str(job.get("status")).lower() == "completed":
            rec["files"] = save(job, output_dir, filename_prefix, cfg)
            if not rec["files"]:
                rec["ok"] = False
                rec["error"] = "completed but no file saved"
        elif job.get("timed_out"):
            rec["ok"] = False
            rec["error"] = f"timed out after {rec['elapsed_s']}s; the job is still running, " \
                           f"poll job_status('{sub['job_id']}')"
        else:
            rec["ok"] = False
            err = job.get("error") or {}
            rec["error"] = err.get("message") if isinstance(err, dict) else str(err)
    return rec


def status(job_id: str, cfg: dict | None = None, *, output_dir: str | None = None,
           filename_prefix: str = "LocModel_vid", save_result: bool = True) -> dict:
    cfg = cfg or core.load_config()
    job = fetch(job_id, cfg)
    out = {"ok": True, "job_id": job_id, "status": job.get("status"),
           "error": job.get("error"), "progress": job.get("progress"),
           "queue_position": job.get("queue_position"),
           "cancel_requested": job.get("cancel_requested"), "files": []}
    if str(job.get("status")).lower() == "completed" and save_result:
        out["files"] = save(job, output_dir, filename_prefix, cfg)
    return out


def recipes(kind: str | None = None, cfg: dict | None = None) -> dict:
    """List engine recipes; ``kind="video"`` keeps only the video ones."""
    cfg = cfg or core.load_config()
    base, _port, _src = core.base_url(cfg=cfg)
    code, obj = core.http_json(base + RECIPES_PATH, timeout=60)
    if code == 404:
        raise VideoError("GET /sdcpp/v1/recipes -> 404: this engine build has no recipe API "
                         "(added in LocModel 3.6.x)", "recipes_unsupported")
    if code is None or code >= 400:
        raise VideoError(f"cannot list recipes ({code}): {obj}", "engine_unreachable")
    items = obj.get("recipes") if isinstance(obj, dict) else obj
    items = items or []
    video_hint = ("video", "wan", "ltx", "hunyuan", "minimax", "hyperflow", "lingbot",
                  "modal", "s2v", "t2v", "i2v")
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id") or item.get("name") or "")
        modes = item.get("modes") or item.get("supported_modes") or []
        is_video = "vid_gen" in modes or any(h in rid.lower() for h in video_hint)
        if kind == "video" and not is_video:
            continue
        rows.append({
            "id": rid,
            "name": item.get("name"),
            "is_video": bool(is_video),
            "modes": modes,
            "size_multiple": item.get("size_multiple"),
            "required_any": item.get("required_any"),
            "slots": [s.get("key") or s.get("id") for s in (item.get("slots") or [])
                      if isinstance(s, dict)],
            "defaults": item.get("defaults"),
        })
    return {"ok": True, "count": len(rows), "recipes": rows}


# ---------------------------------------------------------------------------
# CLI (thin wrapper; MCP is the primary surface)
# ---------------------------------------------------------------------------

def cli(argv=None) -> int:
    """``locmodel_image.py video ...`` -> stable-diffusion.cpp ``vid_gen`` bridge."""
    import argparse

    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    if cmd in ("-h", "--help", "help"):
        print(__doc__ or "locmodel video bridge")
        print(__CLI_USAGE__)
        return 0
    out = {"ok": False}
    try:
        if cmd == "status":
            cfg = core.load_config()
            out = {"ok": True, "mode": engine_mode(cfg), "capabilities": capabilities(cfg)}
        elif cmd == "generate":
            ap = argparse.ArgumentParser(prog="locmodel_image.py video generate")
            ap.add_argument("--prompt", required=True)
            ap.add_argument("--negative", default="")
            ap.add_argument("--width", type=int, default=832)
            ap.add_argument("--height", type=int, default=480)
            ap.add_argument("--fps", type=int)
            ap.add_argument("--frames", type=int, dest="video_frames")
            ap.add_argument("--steps", type=int)
            ap.add_argument("--flow-shift", type=float, dest="flow_shift")
            ap.add_argument("--cfg", type=float, dest="cfg_scale")
            ap.add_argument("--scheduler")
            ap.add_argument("--sampler", dest="sample_method")
            ap.add_argument("--seed", type=int, default=-1)
            ap.add_argument("--format", dest="output_format",
                            choices=list(OUTPUT_FORMATS))
            ap.add_argument("--init-image", dest="init_image")
            ap.add_argument("--end-image", dest="end_image")
            ap.add_argument("--ref-image", dest="ref_images", action="append")
            ap.add_argument("--out", dest="output_dir")
            ap.add_argument("--wait", type=float, default=0,
                            help="seconds to wait before returning (0 = return job_id)")
            ap.add_argument("--timeout", type=float, default=1800)
            ap.add_argument("--dry-run", action="store_true")
            a = ap.parse_args(argv[1:])
            out = generate(
                a.prompt, core.load_config(), negative_prompt=a.negative,
                width=a.width, height=a.height, fps=a.fps, video_frames=a.video_frames,
                steps=a.steps, flow_shift=a.flow_shift, cfg_scale=a.cfg_scale,
                scheduler=a.scheduler, sample_method=a.sample_method, seed=a.seed,
                output_format=a.output_format, init_image=a.init_image,
                end_image=a.end_image, ref_images=a.ref_images,
                wait_seconds=a.wait, timeout=a.timeout, output_dir=a.output_dir,
                dry_run=a.dry_run)
        elif cmd == "job":
            ap = argparse.ArgumentParser(prog="locmodel_image.py video job")
            ap.add_argument("job_id")
            ap.add_argument("--out", dest="output_dir")
            ap.add_argument("--no-save", action="store_true")
            a = ap.parse_args(argv[1:])
            out = status(a.job_id, core.load_config(), output_dir=a.output_dir,
                         save_result=not a.no_save)
        elif cmd == "cancel":
            ap = argparse.ArgumentParser(prog="locmodel_image.py video cancel")
            ap.add_argument("job_id")
            a = ap.parse_args(argv[1:])
            out = cancel(a.job_id, core.load_config())
        elif cmd == "recipes":
            ap = argparse.ArgumentParser(prog="locmodel_image.py video recipes")
            ap.add_argument("--kind", default="video", choices=["video", "all"])
            a = ap.parse_args(argv[1:])
            out = recipes(None if a.kind == "all" else "video", core.load_config())
        else:
            print(__CLI_USAGE__)
            return 2
    except core.BridgeError as exc:
        print(json.dumps({"ok": False, "error": getattr(exc, "code", "bridge_error"),
                          "message": str(exc),
                          "hint": getattr(exc, "hint", "")},
                         ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 2


__CLI_USAGE__ = """\
usage: locmodel_image.py video <command>

  status                        engine mode + capabilities (is vid_gen available?)
  generate --prompt "..."       submit a clip; add --wait 600 to block until done
           [--init-image IMG] [--fps 16] [--frames 9] [--format webm]
  job <job_id> [--out DIR]      poll a job and save the finished file
  cancel <job_id>               cancel a queued / running job
  recipes [--kind video|all]    list engine recipes (3.6.x only)
"""



__all__ = ["VideoError", "generate", "status", "submit", "wait", "cancel", "fetch",
           "recipes", "build_payload", "engine_mode", "require_video_mode", "save",
           "capabilities", "cli", "OUTPUT_FORMATS"]
