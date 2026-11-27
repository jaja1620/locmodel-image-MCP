#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LocModel Image MCP Server  (v3.0.0)

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

try:
    import locmodel_edit as editor  # noqa: E402
except Exception as _exc:  # pragma: no cover - surfaced through tool errors
    editor = None
    _EDIT_IMPORT_ERROR = _exc
else:
    _EDIT_IMPORT_ERROR = None

try:
    import locmodel_video as video  # noqa: E402  (引擎原生 vid_gen：GUI 视频→生成视频)
except Exception as _exc:  # pragma: no cover - surfaced through tool errors
    video = None
    _VIDEO_IMPORT_ERROR = _exc
else:
    _VIDEO_IMPORT_ERROR = None

try:
    import locmodel_lipsync as lipsync  # noqa: E402  (GUI 视频→对口型视频 独立管线)
except Exception as _exc:  # pragma: no cover - surfaced through tool errors
    lipsync = None
    _LIPSYNC_IMPORT_ERROR = _exc
else:
    _LIPSYNC_IMPORT_ERROR = None

SERVER_NAME = "locmodel-image"
SERVER_VERSION = core.VERSION if core else "4.0.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2024-11-05"


def log(msg: str) -> None:
    sys.stderr.write(f"[locmodel-mcp] {msg}\n")
    sys.stderr.flush()


def _policy_text() -> str:
    """Render size policy from the single source of truth (core.DEFAULTS)."""
    if not core:
        return ("auto-sized by the tool: 768k-pixel minimum area, long edge <= 1536px; "
                "an explicit width/height is honoured (engine limit 4096px)")
    cfg = core.load_config()
    pol = core._size_policy(cfg, None)
    eng = pol["engine"]
    return (f"when the tool picks the size: {pol['min_area'] // 1024}k-pixel minimum area, "
            f"long edge <= {pol['max_side']}px, multiple of {pol['multiple']}. "
            f"An explicit width/height is honoured as requested -- only the engine's hard "
            f"limits ({eng['min_edge']}-{eng['max_width']}px per edge) can shrink it, and "
            f"big canvases come back with a VRAM warning instead of being resized")


def _ref_props() -> dict:
    """Properties shared by every reference-image entry point."""
    return {
        "ref_images": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 8,
            "description": (
                "Reference images (LocModel 3.6+ ref channel). Each entry: an absolute file "
                "path, a data URI, or bare base64. The FIRST one anchors identity and, with "
                "ref_fit='follow_first', its framing sets the canvas. Only the plural name "
                "works: a singular ref_image is silently dropped by the engine and the job "
                "degrades into plain text-to-image."
            ),
        },
        "ref_fit": {
            "type": "string",
            "enum": ["follow_first", "image"],
            "description": "follow_first (default) derives the canvas from reference #1; "
                           "image keeps the width/height (or aspect) you pass.",
        },
        "ref_max_pixels": {
            "type": "integer",
            "description": "Pixel budget when the canvas is derived from reference #1. "
                           "Default 1048576 (1024x1024); allowed 65536-4194304. 1.57 MP was "
                           "measured safe on flux-2-klein-4b, above that VRAM runs out.",
        },
    }


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
            "width": {"type": "integer",
                      "description": "Pixel width. Used exactly as given (multiples of 8 are "
                                     "cleanest); only the engine's own limit (64-4096 per edge) "
                                     "can change it. Omitted = the engine default."},
            "height": {"type": "integer",
                       "description": "Pixel height. Same contract as width. Omitted = the "
                                      "engine default."},
            "aspect": {
                "type": "string",
                "enum": list(core.ASPECTS) if core else ["1:1", "16:9", "9:16", "4:3", "3:4"],
                "description": "Aspect preset used when width/height are omitted; the tool then "
                               "sizes the image itself and applies its VRAM-aware long edge.",
            },
            "steps": {"type": "integer", "description": "Sampling steps. Defaults to the engine's own default."},
            "sampler": {"type": "string", "description": "Sampler name, e.g. euler / dpm++2m."},
            "seed": {"type": "integer", "description": "-1 for random; a fixed seed reproduces an image."},
            "count": {"type": "integer", "description": "Number of images (1-4 recommended)."},
            "output_dir": {
                "type": "string",
                "description": "Absolute directory for the image files. Omit to use the working "
                               "directory's ./output folder.",
            },
            "filename_prefix": {"type": "string", "description": "Semantic filename prefix, e.g. architecture_court."},
            "model": {"type": "string", "description": "Model override for multi-model engines."},
            "cfg": {"type": "number", "description": "Guidance / CFG scale for this call. Same as params.cfg."},
            "params": {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Any workflow parameter, applied to THIS call only. Names: cfg, img_cfg, "
                    "steps, sampler, scheduler, flow_shift, shifted_timestep, eta, "
                    "distilled_guidance, slg_scale, clip_skip, strength, seed, count. "
                    "Folded groups: slg_layers/slg_layer_start/slg_layer_end, "
                    "hires_enabled/hires_upscaler/hires_target_width/hires_target_height/"
                    "hires_steps/hires_denoising_strength/hires_upscale_tile_size/"
                    "hires_scale/hires_custom_sigmas (upscaler names are engine spellings, "
                    "e.g. 'Latent (nearest)'), "
                    "vae_tiling/vae_tile_size_w/vae_tile_size_h/vae_rel_size_w/vae_rel_size_h/"
                    "vae_target_overlap/vae_temporal_tiling/vae_extra_tiling_args, "
                    "output_format (png/jpeg/webp)/output_compression, "
                    "high_noise_steps/high_noise_cfg/high_noise_sampler, "
                    "custom_sigmas/extra_sample_args/lora. Values travel in the HTTP payload "
                    "only -- nothing is written to LocModel's config."
                ),
            },
            **_ref_props(),
            "timeout_s": {"type": "integer", "description": "Job timeout in seconds."},
            "guard": {"type": "boolean",
                      "description": "Keep the automatic size guard (area floor, VRAM-aware long "
                                     "edge) for a size the tool picks itself. Set false to submit "
                                     "exactly the size given. Explicit width/height is honoured "
                                     "either way; only the engine's hard limits still apply."},
            "dry_run": {"type": "boolean",
                        "description": "Validate and build the payload, return it with the size "
                                       "notes and the live engine limits, submit nothing. Use it "
                                       "to confirm how a parameter or size will land."},
        },
        "required": ["prompt"],
    }


_REF_EDIT_KEYS = ("prompt", "negative_prompt", "width", "height", "aspect", "steps",
                  "sampler", "seed", "count", "output_dir", "filename_prefix", "model",
                  "cfg", "params", "timeout_s", "guard", "dry_run")


def _ref_edit_schema() -> dict:
    """Focused schema: the ref channel always wins the sizing argument."""
    props = dict(_generate_schema()["properties"])
    props.update(_ref_props())
    return {
        "type": "object",
        "properties": {key: props[key] for key in _REF_EDIT_KEYS if key in props},
        "required": ["prompt", "ref_images"],
    }


TOOLS = [
    {
        "name": "generate_image",
        "description": (
            "Generate images with the LOCAL LocModel / stable-diffusion.cpp engine "
            "(offline, no cloud cost). Pass an already-optimised English prompt; the tool "
            "returns absolute image paths plus the seed used for reproduction. "
            f"Sizing: {_policy_text()}. One 1024x1024 image takes roughly 20-60 s on a "
            "mid-range GPU."
        ),
        "inputSchema": _generate_schema(),
    },
    {
        "name": "ref_edit",
        "description": (
            "Edit a photo by reference: same local engine, but the ref_images channel is "
            "forced on, which keeps the subject's identity intact instead of re-imagining it "
            "(a plain generate_image with one reference also works; this tool simply cannot "
            "fall back). Describe ONLY the change you want -- 'same person, change the "
            "hairstyle to a short bob'. The canvas follows reference #1 by default. Strength, "
            "mask and negative prompt do not exist on this channel; use redraw_image when you "
            "need them."
        ),
        "inputSchema": _ref_edit_schema(),
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


def _regions_schema() -> dict:
    return {
        "type": "object",
        "description": (
            "Areas to repaint, drawn on the CPU into a greyscale PNG mask. White = repaint, "
            "black = keep. Use this when you have no mask file. Example: "
            '{"unit":"norm","regions":[{"type":"rect","x":[0.1,0.4,0.6,0.9]}]}.'
        ),
        "properties": {
            "unit": {
                "type": "string",
                "enum": ["norm", "px"],
                "description": "norm = fractions 0..1 of the image (default); px = pixels.",
            },
            "invert": {"type": "boolean", "description": "Invert the finished mask."},
            "regions": {
                "type": "array",
                "description": "Region list. Several regions are merged with OR.",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["rect", "polygon", "brush", "circle", "color"],
                            "description": "rect (default) | polygon | brush | circle | color.",
                        },
                        "x": {
                            "type": "array",
                            "items": {"type": "number"},
                            "description": "rect: [x1, y1, x2, y2].",
                        },
                        "points": {
                            "type": "array",
                            "description": "polygon: [[x,y], ...]; brush: stroke points; "
                                           "circle: [[cx,cy]].",
                            "items": {"type": "array", "items": {"type": "number"}},
                        },
                        "radius": {
                            "type": "number",
                            "description": "brush/circle radius (norm = fraction of the short side).",
                        },
                        "rgb": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "color: [r, g, b] to select, e.g. a green screen.",
                        },
                        "tolerance": {"type": "integer", "description": "color: 0-255 tolerance."},
                    },
                },
            },
            "boxes": {
                "type": "array",
                "description": "Shorthand for several rects: [[x1,y1,x2,y2], ...].",
                "items": {"type": "array", "items": {"type": "number"}},
            },
        },
    }


def _edit_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "init_image": {
                "type": "string",
                "description": "Absolute path of the image to modify (PNG or JPEG).",
            },
            "prompt": {
                "type": "string",
                "description": "The change to make, described in English. Say WHAT should appear "
                               "in the masked area, not what to 'edit'.",
            },
            "mask_image": {
                "type": "string",
                "description": "Absolute path of a mask PNG (white = repaint). Rescaled to the "
                               "image size when needed. Use the bundled annotator to draw one.",
            },
            "regions": _regions_schema(),
            "strength": {
                "type": "number",
                "description": "Denoising strength 0..1 (default 0.75). Low keeps the original "
                               "look, high follows the prompt harder.",
            },
            "negative_prompt": {"type": "string", "description": "What to avoid inside the mask."},
            "model": {
                "type": "string",
                "description": "Model to use. Omit to let the tool pick by priority "
                               "(qwen-image-edit > flux2-klein > qwen-image).",
            },
            "steps": {"type": "integer", "description": "Sampling steps. Defaults to the engine's own."},
            "sampler": {"type": "string", "description": "Sampler name, e.g. euler."},
            "cfg_scale": {"type": "number", "description": "CFG / guidance scale."},
            "seed": {"type": "integer", "description": "-1 for random."},
            "mask_blur": {"type": "integer", "description": "Mask feather in pixels (engine side)."},
            "inpaint_padding": {"type": "integer", "description": "Extra context pixels around the mask."},
            "output_dir": {"type": "string", "description": "Absolute output directory."},
            "filename_prefix": {"type": "string", "description": "Filename prefix."},
            "channel": {
                "type": "string",
                "enum": ["auto", "img_gen", "img2img"],
                "description": "Engine endpoint. auto tries /sdcpp/v1/img_gen first, then "
                               "/sdapi/v1/img2img.",
            },
            "timeout_s": {"type": "integer", "description": "Job timeout in seconds (default 900)."},
            "verify": {
                "type": "string",
                "enum": ["agent", "off"],
                "description": "agent = the calling agent must inspect the returned image.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Build the mask and payload, call nothing. Use it to check "
                               "coordinates cheaply.",
            },
        },
        "required": ["init_image", "prompt"],
    }


def _redraw_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "init_image": {"type": "string", "description": "Absolute path of the source image."},
            "prompt": {
                "type": "string",
                "description": "Describe the WHOLE picture you want, keeping the composition of "
                               "the source.",
            },
            "strength": {
                "type": "number",
                "description": "Denoising strength 0..1 (default 0.6). Under ~0.35 the engine "
                               "barely changes anything.",
            },
            "negative_prompt": {"type": "string", "description": "What to avoid."},
            "model": {"type": "string", "description": "Model override."},
            "steps": {"type": "integer", "description": "Sampling steps."},
            "sampler": {"type": "string", "description": "Sampler name."},
            "cfg_scale": {"type": "number", "description": "CFG / guidance scale."},
            "seed": {"type": "integer", "description": "-1 for random."},
            "output_dir": {"type": "string", "description": "Absolute output directory."},
            "filename_prefix": {"type": "string", "description": "Filename prefix."},
            "channel": {"type": "string", "enum": ["auto", "img_gen", "img2img"]},
            "timeout_s": {"type": "integer", "description": "Job timeout in seconds."},
            "verify": {"type": "string", "enum": ["agent", "off"]},
            "dry_run": {"type": "boolean", "description": "Validate inputs only, call nothing."},
        },
        "required": ["init_image", "prompt"],
    }


def _set_params_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "params": {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Parameter names and values to remember for the rest of this session, "
                    "e.g. {\"cfg\": 3.5, \"scheduler\": \"karras\", \"steps\": 12}. "
                    "Applies to every later generate_image / edit_image / redraw_image call "
                    "that does not set the same knob itself. Lives in the MCP process memory "
                    "only: nothing is written to disk, and it disappears when the MCP "
                    "process restarts."
                ),
            },
            "merge": {"type": "boolean", "description": "true (default) adds to the current set, false replaces it."},
        },
        "required": ["params"],
    }


def _get_params_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "include_folded": {"type": "boolean", "description": "Also list the folded groups (hires, vae_tiling, output, high_noise, raw, slg)."},
            "live_defaults": {"type": "boolean", "description": "Read defaults from the running engine instead of the built-in fallback table."},
        },
    }


TOOLS += [
    {
        "name": "set_workflow_params",
        "description": (
            "Remember workflow parameters for the rest of this session (CFG, steps, sampler, "
            "scheduler, flow_shift, eta, hi-res fix, VAE tiling, LoRA ...). Use it for batch "
            "runs where every image shares the same look, instead of repeating params on each "
            "call. Values are sent in the HTTP payload per job -- they never touch LocModel's "
            "config.json, which the GUI rewrites. Returns the effective parameter table with "
            "defaults, ranges and the source of each default. Call reset_workflow_params to "
            "drop everything and fall back to the LocModel GUI panel settings."
        ),
        "inputSchema": _set_params_schema(),
    },
    {
        "name": "get_workflow_params",
        "description": (
            "List every workflow parameter this tool accepts, with its default value, valid "
            "range / options, the payload path it maps to, and whether the default came from "
            "the live engine or from the built-in fallback. Use it before setting parameters "
            "so you do not guess a name or a range."
        ),
        "inputSchema": _get_params_schema(),
    },
    {
        "name": "reset_workflow_params",
        "description": (
            "Clear the session parameter set (all of it, or the listed keys) so generation goes "
            "back to the LocModel GUI panel settings. This is the one-command restore."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keys": {"type": "array", "items": {"type": "string"}, "description": "Only clear these names; omit to clear everything."}
            },
        },
    },
    {
        "name": "edit_image",
        "description": (
            "EDIT an existing local image: repaint only the masked area (inpainting) while the "
            "rest stays pixel-identical -- the tool re-composes the result on the CPU, so "
            "outside-mask pixels cannot drift. Give either mask_image or regions. Models are "
            "chosen by priority (qwen-image-edit > flux2-klein > qwen-image); the loaded model "
            "wins when it can region-edit. Returns absolute PNG paths for the agent to inspect."
        ),
        "inputSchema": _edit_schema(),
    },
    {
        "name": "redraw_image",
        "description": (
            "REDRAW an existing local image from a new prompt, keeping its composition and size "
            "(whole-image img2img). The character of the picture changes but the geometry stays. "
            "Use edit_image instead when only part of the picture should change."
        ),
        "inputSchema": _redraw_schema(),
    },
    {
        "name": "list_edit_models",
        "description": (
            "List every model that can be used for editing, with family, priority rank, whether "
            "it supports region editing, and which one would be picked automatically right now. "
            "Call this before editing when you are unsure what is installed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "models_dir": {
                    "type": "string",
                    "description": "Extra directory to scan for model files (optional).",
                },
                "refresh": {
                    "type": "boolean",
                    "description": "Ignore the cached snapshot and probe the engine again "
                                   "(optional, default false).",
                },
            },
        },
    },
    {
        "name": "load_model",
        "description": (
            "Load / switch the model in the local engine, and wait until the engine reports it. "
            "Use it when the model you need for an edit is not the one currently loaded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Model name or filename as listed by "
                                                           "list_edit_models / list_models."},
                "timeout_s": {"type": "integer", "description": "Seconds to wait for the load "
                                                               "(default 240)."},
            },
            "required": ["model"],
        },
    },
    {
        "name": "lipsync_status",
        "description": (
            "Check whether LocModel's built-in lipsync (对口型视频) pipeline is ready: GUI sub-panel "
            "视频 → 对口型视频, InfiniteTalk + Wan2.1-I2V-14B-480P, its own venv and model dir. "
            "This pipeline is INDEPENDENT from the sd-server engine, so engine_doctor / locmodel_status "
            "do not cover it. Call this before lipsync_generate; if the dedicated models are not "
            "mounted inside LocModel the whole task fails and there is no fallback."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean", "description": "Re-scan the model dir (default true)."},
                "models_dir": {"type": "string", "description": "Override the lipsync model root."},
            },
        },
    },
    {
        "name": "lipsync_generate",
        "description": (
            "Generate a singing / talking head video: one portrait image + one audio file -> mp4 whose "
            "mouth follows the song. Same pipeline as GUI 视频 → 对口型视频 (InfiniteTalk, "
            "Wan2.1-I2V-14B-480P fp8 or GGUF, Demucs vocal separation, chinese-wav2vec2). Requires the "
            "dedicated models and runtime downloaded and mounted inside LocModel first (about 24.2 GB "
            "models + 6 GB CUDA torch); without them the job fails outright. Slow: a 1-minute song "
            "takes tens of minutes even on a 4090."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "image": {"type": "string", "description": "Absolute path to a portrait photo "
                                                          "(jpg/png/webp/bmp)."},
                "audio": {"type": "string", "description": "Absolute path to the song or voice track "
                                                          "(mp3/wav/m4a/flac/ogg/aac)."},
                "prompt": {"type": "string", "description": "Scene / behaviour description, e.g. "
                                                           "'a young woman singing warmly to the "
                                                           "camera in a cafe'. Default 'a person is "
                                                           "singing and speaking to the camera'."},
                "negative": {"type": "string", "description": "Negative prompt (default: LocModel's "
                                                             "built-in one)."},
                "res": {"type": "string", "enum": ["480", "720", "custom"],
                        "description": "Resolution bucket: 480 (default, fits 8 GB VRAM) / 720 / "
                                       "custom (then width+height are required)."},
                "width": {"type": "integer", "description": "Only for res=custom; rounded to 16, "
                                                            "range 256..1920."},
                "height": {"type": "integer", "description": "Only for res=custom; rounded to 16, "
                                                             "range 256..1920."},
                "crop": {"type": ["string", "object"],
                         "description": "'full' (default) / 'head' (upper-middle square, best for "
                                        "talking heads) / 'upper' (upper half) or {x,y,w,h} "
                                        "normalised 0..1."},
                "steps": {"type": "integer", "description": "Sampling steps (default 4 with accel, "
                                                           "40 without)."},
                "accel": {"type": "boolean", "description": "Use the lightx2v distill LoRA (default "
                                                           "true -> 4 steps)."},
                "seed": {"type": "integer", "description": "Seed, -1 = random (default -1)."},
                "max_seconds": {"type": "number", "description": "Render only the first N seconds of "
                                                                "the audio (0 = whole song)."},
                "vocals": {"type": "string", "enum": ["auto", "none"],
                           "description": "auto (default) separates the vocals with Demucs to drive "
                                          "the mouth; none uses the raw audio."},
                "color_fix": {"type": "number",
                              "description": "Colour-correction strength, 1.0 = LocModel default."},
                "crf": {"type": "integer",
                        "description": "H.264 CRF for the mp4, default 18 (LocModel's GUI ships 17); "
                                       "higher = smaller and softer."},
                "vae_tiled": {"type": "boolean",
                              "description": "Tiled VAE decode to fit smaller VRAM (default false)."},
                "sage": {"type": "boolean",
                         "description": "Use SageAttention when the venv has it (default true)."},
                "output_dir": {"type": "string", "description": "Folder for the mp4 (default: "
                                                               "config output_dir or "
                                                               "~/Pictures/LocModel)."},
                "timeout_s": {"type": "integer", "description": "Hard timeout in seconds (default: no "
                                                               "limit; long songs take a long time)."},
                "dry_run": {"type": "boolean", "description": "Assemble and validate the job without "
                                                             "running inference."},
            },
            "required": ["image", "audio"],
        },
    },
]


_LIPSYNC_KEEP = ("ok", "version", "sub_panel", "variant", "paths", "models", "missing",
                 "hardware", "notes", "mount_required", "mount_hint", "blocking", "report")


def _lipsync_slim(res: dict) -> dict:
    """Drop the verbose fact tables; keep what an agent needs to act on."""
    slim = {k: res[k] for k in _LIPSYNC_KEEP if k in res}
    for k, v in res.items():
        if k not in slim and k != "summary" and not isinstance(v, (dict, list)):
            slim[k] = v
    if "pipeline_facts" in res:
        slim["pipeline"] = res.get("pipeline")
        slim["host_commands"] = res["pipeline_facts"].get("host_commands")
    return slim


def _status_text(res: dict) -> dict:
    """Compact, path-first report: MCP transports truncate long payloads, so the
    paths an agent must act on go to the top instead of inside a nested JSON."""
    lines = []
    if res.get("summary"):
        lines.append(str(res["summary"]))
    paths = res.get("paths") or {}
    for key in ("models_root", "lipsync_root", "python", "code_dir", "runner", "jobs_root"):
        if paths.get(key):
            lines.append(f"{key}: {paths[key]}")
    found = {k: v for k, v in (res.get("models") or {}).items() if v}
    if found:
        short = []
        for k, v in found.items():
            name = os.path.basename(str(v).rstrip("\\/")) or str(v)
            short.append(f"{k}={name}")
        lines.append("found: " + ", ".join(short))
    else:
        lines.append("found: none")
    missing = res.get("missing") or []
    if missing:
        lines.append("missing: " + "; ".join(
            "%s(%s)" % (i.get("key"), i.get("need")) for i in missing))
    var = res.get("variant") or {}
    if var:
        lines.append("variant: %s / %s" % (var.get("variant"), var.get("reason")))
    hw = res.get("hardware") or {}
    if hw:
        lines.append("hardware: ram=%s GB, vram=%s GB" % (hw.get("ram_gb"), hw.get("vram_gb")))
    for note in (res.get("notes") or []):
        lines.append(f"note: {note}")
    if res.get("blocking"):
        lines.append(f"blocking: {res['blocking']}")
    if res.get("host_commands"):
        lines.append("host_commands: " + " | ".join(res["host_commands"]))
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def _lipsync_text(res: dict) -> dict:
    """Summary first (what an agent reads), full structured payload after it."""
    out = dict(res)
    summary = out.pop("summary", "")
    body = json.dumps(out, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": f"{summary}\n\n{body}" if summary else body}]}


def _lipsync_call(name: str, args: dict) -> dict:
    """Handle the lipsync tools. They never touch the sd-server engine."""
    if lipsync is None:  # pragma: no cover
        return _error(f"lipsync module unavailable: {_LIPSYNC_IMPORT_ERROR}")
    cfg = core.load_config() if core else {}
    try:
        if name == "lipsync_status":
            res = lipsync.status(cfg, refresh=bool(args.get("refresh", True)),
                                 models_dir=args.get("models_dir"))
            if not res.get("ok"):
                need = "; ".join("%s (%s)" % (i.get("need"), i.get("where"))
                                 for i in (res.get("missing") or [])[:6])
                res["summary"] = (
                    "NOT READY - 未在 LocModel 挂载专用模型将导致整体任务失败。\n"
                    f"missing: {need or 'unknown'}\n"
                    f"how to fix: {lipsync.MOUNT_HINT}"
                )
                # pipeline_facts 全文太长会把关键路径挤出 agent 的可见窗口，这里只留一行
                return _status_text(res)
            rep = res.get("report") or {}
            var = rep.get("variant") or {}
            lines = [
                "READY - 对口型视频管线可用",
                f"variant: {var.get('variant')} ({var.get('label')})",
                f"python: {(rep.get('paths') or {}).get('python')}",
                f"models_root: {(rep.get('paths') or {}).get('models_root')}",
                f"pipeline: {res.get('pipeline')}",
                f"gui: {lipsync.PIPELINE_FACTS['gui_path']}",
            ]
            res["summary"] = "\n".join(lines)
            return _status_text(res)

        image = args.get("image") or ""
        audio = args.get("audio") or ""
        if not image or not audio:
            return _error("Missing required arguments: image and audio.")
        res = lipsync.generate(
            image, audio, cfg,
            prompt=args.get("prompt", ""),
            negative=args.get("negative", ""),
            res=str(args.get("res") or "480"),
            width=args.get("width"),
            height=args.get("height"),
            crop=args.get("crop"),
            steps=args.get("steps"),
            accel=bool(args.get("accel", True)),
            seed=int(args.get("seed", -1)),
            max_seconds=float(args.get("max_seconds") or 0),
            vocals=str(args.get("vocals") or "auto"),
            output_dir=args.get("output_dir"),
            timeout_s=int(args.get("timeout_s") or 0),
            dry_run=bool(args.get("dry_run", False)),
        )
        if res.get("dry_run"):
            lines = ["dry run: job assembled, inference not started",
                     f"job: {res['job_json']}",
                     f"would write: {res['output']}",
                     f"variant: {res['variant']} | res: {res['res']} | steps: {res['steps']}"]
            res["summary"] = "\n".join(lines)
            return _lipsync_text(res)
        if not res.get("ok"):
            detail = res.get("detail") or ""
            return _error((f"Lipsync failed: {res.get('message')}\n{detail}").strip(),
                          res.get("hint"))
        lines = [f"video: {res['output']}",
                 f"variant: {res['variant']} | res: {res['res']} | steps: {res['steps']}",
                 f"elapsed: {res.get('elapsed_s')}s | video_seconds: {res.get('video_seconds')}"]
        if res.get("stages"):
            lines.append("stages: " + " -> ".join("%s(%s%%)" % (s.get("stage"), s.get("pct"))
                                                 for s in res["stages"][-8:]))
        res["summary"] = "\n".join(lines)
        return _lipsync_text(res)
    except lipsync.LipsyncError as exc:
        return _error(str(exc), getattr(exc, "hint", ""))
    except Exception as exc:
        log(traceback.format_exc())
        return _error(f"{type(exc).__name__}: {exc}")


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


def _wf_table(cfg: dict, include_folded: bool = False, live: bool = True) -> list:
    """Parameter table for the caller, with live engine defaults when reachable."""
    caps: dict = {}
    if live:
        try:
            base, port, _src = core.base_url(cfg=cfg)
            if port:
                caps = core.capabilities(base, cfg) or {}
        except Exception:  # engine down is not an error for a documentation call
            caps = {}
    return core.workflow.table(caps, core.workflow.session_get(),
                               include_folded=include_folded)


TOOLS += [
    {
        "name": "generate_video",
        "description": (
            "Generate a video with the engine-native endpoint POST /sdcpp/v1/vid_gen — this is "
            "LocModel GUI 视频 → 生成视频 (text/image to video, e.g. Wan2.2-TI2V, Wan2.1, LTX), NOT "
            "the lip-sync sub-panel. Returns a job_id immediately (wait_seconds=0) because a clip "
            "takes minutes; poll with job_status. Do NOT use it for 对口型视频 -> use lipsync_generate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Text prompt describing the shot."},
                "negative_prompt": {"type": "string", "description": "Negative prompt."},
                "width": {"type": "integer", "description": "Default 832; multiple of 8, 64..4096."},
                "height": {"type": "integer", "description": "Default 480; multiple of 8, 64..4096."},
                "fps": {"type": "integer", "description": "Default from the engine (16 for Wan2.2-TI2V)."},
                "video_frames": {"type": "integer",
                                 "description": "Frame count; the engine rounds it to the recipe's "
                                                "frame_step (4/8/17)."},
                "steps": {"type": "integer", "description": "Sampling steps (engine default 4)."},
                "flow_shift": {"type": "number", "description": "Flow shift (engine default 12.0)."},
                "cfg_scale": {"type": "number", "description": "maps to sample_params.guidance.txt_cfg."},
                "scheduler": {"type": "string",
                              "description": "'discrete' / 'karras' / 'exponential' ... 'default' is "
                                             "rejected by the engine."},
                "sample_method": {"type": "string", "description": "Sampler name, e.g. 'euler'."},
                "custom_sigmas": {"type": "array", "items": {"type": "number"},
                                  "description": "Numeric sigma array (numeric only; names/CSV are 400)."},
                "seed": {"type": "integer", "description": "-1 / omit for random."},
                "output_format": {"type": "string", "enum": ["webm", "webp", "avi"],
                                  "description": "Default webm."},
                "init_image": {"type": "string",
                               "description": "Local image path / data URI / base64 (i2v, first frame)."},
                "end_image": {"type": "string",
                              "description": "Last frame for flf2v recipes."},
                "ref_images": {"type": "array", "items": {"type": "string"},
                               "description": "Reference images (data URI / path)."},
                "control_frames": {"type": "array", "items": {"type": "string"},
                                   "description": "VACE control frames."},
                "vace_strength": {"type": "number", "description": "VACE strength."},
                "moe_boundary": {"type": "number",
                                 "description": "MoE boundary for two-expert models (Wan2.2 A14B)."},
                "high_noise_steps": {"type": "integer",
                                     "description": "Two-pass sampling; NOT supported by "
                                                    "Wan2.2-TI2V (400)."},
                "vae_tiling": {"type": "boolean", "description": "Enable VAE tiling to save VRAM."},
                "wait_seconds": {"type": "number",
                                 "description": "0 (default) = return job_id immediately; >0 = wait "
                                                "that long then report."},
                "output_dir": {"type": "string", "description": "Where to save the video file."},
                "dry_run": {"type": "boolean", "description": "Only build and return the payload."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "job_status",
        "description": (
            "Poll a vid_gen job created by generate_video (GET /sdcpp/v1/jobs/{id}) and save the "
            "finished video. The engine keeps results for ~600 s only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job id returned by generate_video."},
                "save_result": {"type": "boolean", "description": "Save the file when completed "
                                                                  "(default true)."},
                "output_dir": {"type": "string", "description": "Where to save the video file."},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "cancel_job",
        "description": "Cancel a queued or running vid_gen job (POST /sdcpp/v1/jobs/{id}/cancel). "
                       "LocModel 3.3.0 returns 410, 3.6.x supports it.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "Job id to cancel."}},
            "required": ["job_id"],
        },
    },
    {
        "name": "list_recipes",
        "description": "List engine recipes (GET /sdcpp/v1/recipes) with slots / required_any / "
                       "size_multiple / defaults. kind='video' keeps only video recipes — use it to "
                       "see which video models the engine can drive.",
        "inputSchema": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["video", "all"],
                                    "description": "Filter (default all)."}},
        },
    },
]


def _video_text(res: dict) -> dict:
    out = dict(res)
    summary = out.pop("summary", "")
    body = json.dumps(out, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": f"{summary}\n\n{body}" if summary else body}]}


def _video_call(name: str, args: dict) -> dict:
    """Engine-native video generation (GUI 视频 → 生成视频)."""
    if video is None:  # pragma: no cover
        return _error(f"video module unavailable: {_VIDEO_IMPORT_ERROR}")
    cfg = core.load_config() if core else {}
    try:
        if name == "list_recipes":
            kind = args.get("kind") or "all"
            res = video.recipes(None if kind in ("all", "", None) else kind, cfg)
            rows = res.get("recipes") or []
            head = "\n".join(
                "- %s (slots=%s, required_any=%s)" % (r["id"], ",".join(map(str, r["slots"])) or "-",
                                                     r.get("required_any"))
                for r in rows[:12])
            res["summary"] = (f"{res['count']} recipe(s) (kind={kind}). 视频 recipe 需要 vae/tae "
                              f"槽位与对应的视频主模型；未挂载专用模型将导致整体任务失败。\n{head}")
            return _video_text(res)

        if name == "cancel_job":
            job_id = (args.get("job_id") or "").strip()
            if not job_id:
                return _error("Missing required argument: job_id.")
            res = video.cancel(job_id, cfg)
            res["summary"] = f"cancel requested for {job_id}"
            return _video_text(res)

        if name == "job_status":
            job_id = (args.get("job_id") or "").strip()
            if not job_id:
                return _error("Missing required argument: job_id.")
            res = video.status(job_id, cfg, output_dir=args.get("output_dir"),
                               filename_prefix=args.get("filename_prefix") or "LocModel_vid",
                               save_result=bool(args.get("save_result", True)))
            st = str(res.get("status") or "").lower()
            prog = res.get("progress") or {}
            lines = [f"job {job_id}: {st}",
                     f"progress: stage={prog.get('stage')} pass={prog.get('pass')}/{prog.get('total')} "
                     f"elapsed={prog.get('elapsed_ms')}ms"]
            if res.get("error"):
                lines.append(f"error: {res['error']}")
            if res.get("files"):
                lines.append("saved: " + ", ".join(f["path"] for f in res["files"]))
            elif st == "completed":
                lines.append("completed but nothing saved (result expired or save disabled)")
            res["summary"] = "\n".join(lines)
            return _video_text(res)

        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return _error("Missing required argument: prompt.")
        res = video.generate(
            prompt, cfg,
            negative_prompt=args.get("negative_prompt", ""),
            width=int(args.get("width") or 832),
            height=int(args.get("height") or 480),
            fps=args.get("fps"),
            video_frames=args.get("video_frames"),
            steps=args.get("steps"),
            flow_shift=args.get("flow_shift"),
            cfg_scale=args.get("cfg_scale"),
            scheduler=args.get("scheduler"),
            sample_method=args.get("sample_method"),
            custom_sigmas=args.get("custom_sigmas"),
            seed=args.get("seed"),
            output_format=args.get("output_format"),
            init_image=args.get("init_image"),
            end_image=args.get("end_image"),
            ref_images=args.get("ref_images"),
            control_frames=args.get("control_frames"),
            vace_strength=args.get("vace_strength"),
            moe_boundary=args.get("moe_boundary"),
            high_noise_steps=args.get("high_noise_steps"),
            vae_tiling=args.get("vae_tiling"),
            wait_seconds=float(args.get("wait_seconds") or 0),
            output_dir=args.get("output_dir"),
            dry_run=bool(args.get("dry_run", False)),
        )
        if res.get("dry_run"):
            res["summary"] = "dry_run: payload validated against the live engine, nothing submitted."
            return _video_text(res)
        lines = [f"submitted vid_gen job {res['job_id']} (status={res.get('status')})",
                 f"poll: job_status(job_id='{res['job_id']}')"]
        if res.get("files"):
            lines.append("saved: " + ", ".join(f["path"] for f in res["files"]))
        if res.get("error"):
            lines.append(f"error: {res['error']}")
        if not res.get("ok"):
            lines.append("hint: 该端点由「视频」选项卡的引擎实例提供；引擎不在 vid_gen 模式或"
                         "未挂载视频专用模型时整体失败。")
        res["summary"] = "\n".join(lines)
        return _video_text(res)
    except Exception as exc:  # VideoError and friends
        hint = getattr(exc, "hint", "") or ""
        code = getattr(exc, "code", "") or type(exc).__name__
        return _video_text({"ok": False, "error": str(exc), "code": code,
                            "summary": f"video tool failed [{code}]: {exc}" + (f"\n{hint}" if hint else "")})


def _ref_guard(args: dict) -> str | None:
    """Refuse the two ways a reference call silently degrades.

    Both were measured on LocModel 3.6.5: a singular ref_image produced
    ``fn main()`` with no ref_latents (plain text-to-image), and ref_strength rode
    along without changing the workflow at all.
    """
    params = args.get("params") if isinstance(args.get("params"), dict) else {}
    if "ref_image" in args or "ref_image" in params:
        return ("ref_image is not accepted: the engine reads only the plural ref_images array "
                "and drops a singular key without any error, which turns the job into plain "
                "text-to-image (verified: workflow fn main() with no ref_latents). "
                "Call it as ref_images=['<absolute path>'].")
    if "ref_strength" in args or "ref_strength" in params:
        return ("ref_strength is not supported: on the ref_images channel the engine ignores "
                "strength entirely (the workflow never slices sigmas by it). Use redraw_image "
                "(init_image + strength) when you need a denoising strength, or keep the "
                "engine defaults.")
    return None


def call_tool(name: str, args: dict) -> dict:
    if name in ("lipsync_status", "lipsync_generate"):
        return _lipsync_call(name, args or {})
    if name in ("generate_video", "job_status", "cancel_job", "list_recipes"):
        return _video_call(name, args or {})

    missing = _require_core()
    if missing:
        return _error(missing)

    cfg = core.load_config()

    if name in ("generate_image", "ref_edit"):
        prompt = (args or {}).get("prompt") or ""
        if not prompt.strip():
            return _error("Missing required argument: prompt.")
        refusal = _ref_guard(args or {})
        if refusal:
            return _error(refusal)
        if name == "ref_edit" and not (args or {}).get("ref_images"):
            return _error("ref_edit needs ref_images (1-8 references: a path, a data URI or "
                          "bare base64). Without one it would be plain text-to-image -- use "
                          "generate_image for that.", None)
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
                cfg_scale=args.get("cfg"),
                params=args.get("params"),
                seed=int(args.get("seed", -1)),
                count=int(args.get("count", 1)),
                out_dir=args.get("output_dir"),
                filename_prefix=args.get("filename_prefix") or "locmodel",
                timeout=args.get("timeout_s"),
                model=args.get("model"),
                guard=bool(args.get("guard", True)),
                dry_run=bool(args.get("dry_run", False)),
                ref_images=args.get("ref_images"),
                ref_fit=args.get("ref_fit"),
                ref_max_pixels=args.get("ref_max_pixels"),
            )
        except core.BridgeError as exc:
            return _error(str(exc), exc.hint)
        except Exception as exc:  # pragma: no cover
            log(traceback.format_exc())
            return _error(f"{type(exc).__name__}: {exc}")

        if res.get("dry_run"):
            pol = res.get("size_policy") or {}
            lines = ["dry run: nothing was submitted",
                     f"payload: {json.dumps(res.get('payload') or {}, ensure_ascii=False)}",
                     f"output dir: {res.get('out_dir')}",
                     f"size policy: {json.dumps(pol, ensure_ascii=False)}"]
            if res.get("size_notes"):
                lines.append("notes: " + " | ".join(res["size_notes"]))
            if res.get("ref"):
                ref = res["ref"]
                lines.append(f"reference: {ref['count']} image(s), fit={ref['fit']}, "
                             f"budget={ref['max_pixels']} px")
            out = dict(res)
            out["summary"] = "\n".join(lines)
            return _text(out)

        if not res.get("ok"):
            attempts = res.get("attempts") or []
            tail = "; ".join(f"#{a.get('attempt')} {a.get('error')}" for a in attempts) or "no detail"
            return _error(f"Generation failed: {res.get('message')}. Attempts -> {tail}", res.get("hint"))

        lines = [f"files: {len(res['files'])}", f"size: {res['params']['width']}x{res['params']['height']}",
                 f"seed: {res['params']['seed']}", f"model: {res.get('model')}",
                 f"elapsed: {res['elapsed_s']}s"]
        for f in res["files"]:
            lines.append(f"path: {f['path']}")
        if res.get("ref"):
            ref = res["ref"]
            lines.append(f"reference: {ref['count']} image(s), fit={ref['fit']}, "
                         f"budget={ref['max_pixels']} px (identity anchored by #1)")
        trace = res.get("engine_trace") or {}
        if trace.get("fn") and (res.get("ref") or trace.get("ref_latents")):
            lines.append("engine: " + (trace.get("fn") or "call recorded")
                         + (" | reference latent bound" if trace.get("ref_latents")
                            else " | NO reference latent: plain text-to-image"))
        wf = (res.get("params") or {}).get("workflow") or {}
        if wf:
            lines.append("workflow: " + ", ".join(f"{k}={v}" for k, v in wf.items()))
        if res.get("notes"):
            lines.append("notes: " + " | ".join(res["notes"]))
        out = dict(res)
        out["summary"] = "\n".join(lines)
        return _text(out)

    if name == "set_workflow_params":
        raw = (args or {}).get("params")
        if not isinstance(raw, dict) or not raw:
            return _error("Missing required argument: params (an object of name -> value).")
        try:
            session = core.workflow.session_set(raw, merge=bool(args.get("merge", True)))
        except core.workflow.ParamError as exc:
            return _error(exc.message, exc.hint)
        rows = _wf_table(cfg, include_folded=True)
        lines = ["session parameters now active for this MCP session:"]
        for key, val in session.items():
            lines.append(f"  {key} = {val}")
        lines.append("these win over the LocModel panel settings until reset or restart")
        return _text({"ok": True, "session": session, "params": rows,
                      "summary": "\n".join(lines)})

    if name == "get_workflow_params":
        rows = _wf_table(cfg, include_folded=bool((args or {}).get("include_folded")),
                         live=bool((args or {}).get("live_defaults", True)))
        core_rows = [r for r in rows if r["tier"] == "core"]
        lines = [f"{r['param']} = {r['default']!r}  [{r['source']}]  {r['range']}".rstrip()
                 for r in core_rows]
        return _text({"ok": True, "core": core_rows,
                      "all": rows if (args or {}).get("include_folded") else None,
                      "folded_groups": core.workflow.FOLDED_GROUPS,
                      "session": core.workflow.session_get(),
                      "summary": "\n".join(lines)})

    if name == "reset_workflow_params":
        keys = (args or {}).get("keys")
        try:
            session = core.workflow.session_reset(keys)
        except core.workflow.ParamError as exc:
            return _error(exc.message, exc.hint)
        return _text({"ok": True, "session": session,
                      "summary": "session parameters cleared; generation is back to the "
                                 "LocModel GUI panel settings"})

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

    if name in ("edit_image", "redraw_image", "list_edit_models", "load_model"):
        if editor is None:
            return _error(f"Edit module locmodel_edit.py could not be imported: {_EDIT_IMPORT_ERROR}. "
                          "Keep locmodel_edit.py next to locmodel_image.py.")
        return _call_edit_tool(name, args or {}, cfg)

    return _error(f"Unknown tool: {name}")


def _summary_edit(res: dict) -> str:
    lines = [f"files: {len(res.get('files') or [])}"]
    p = res.get("params") or {}
    lines.append(f"size: {p.get('width')}x{p.get('height')}  strength: {p.get('strength')}  "
                 f"seed: {p.get('seed')}")
    sel = res.get("selection") or {}
    lines.append(f"model: {sel.get('chosen')} ({sel.get('family')}, rank {sel.get('rank')}, "
                 f"{sel.get('reason')})")
    if res.get("channel"):
        lines.append(f"channel: {res['channel']}")
    m = res.get("mask") or {}
    if m:
        lines.append(f"mask: coverage {m.get('coverage')} of the image"
                     + (" (rescaled from the given mask)" if m.get("rescaled") else ""))
    for f in res.get("files") or []:
        lines.append(f"path: {f['path']}")
    for rc in res.get("recomposed") or []:
        lines.append(f"masked: {rc['path']}  ({rc['redrawn_pixels']} px redrawn, "
                     f"{rc['protected_pixels']} px protected)")
    if res.get("notes"):
        lines.append("notes: " + " | ".join(res["notes"]))
    v = res.get("verify") or {}
    if v.get("mode") == "agent":
        lines.append("NEXT: open the image(s) and check the change inside the mask and the "
                     "untouched area around it; retry once with a bigger strength or a tighter "
                     "mask before switching models.")
    return "\n".join(lines)


def _call_edit_tool(name: str, args: dict, cfg: dict) -> dict:
    if name == "list_edit_models":
        try:
            if args.get("models_dir"):
                cfg.setdefault("edit", {})["models_dirs"] = [args["models_dir"]]
            return _text(editor.list_models(cfg, refresh=bool(args.get("refresh"))))
        except editor.EditError as exc:
            return _error(str(exc), exc.hint)
        except Exception as exc:  # pragma: no cover
            log(traceback.format_exc())
            return _error(f"{type(exc).__name__}: {exc}")

    if name == "load_model":
        model = (args.get("model") or "").strip()
        if not model:
            return _error("Missing required argument: model.")
        try:
            res = editor.load_model(cfg, model, timeout=int(args.get("timeout_s") or 240))
        except editor.EditError as exc:
            return _error(str(exc), exc.hint)
        except Exception as exc:  # pragma: no cover
            log(traceback.format_exc())
            return _error(f"{type(exc).__name__}: {exc}")
        res["summary"] = (f"model: {res.get('before')} -> {res.get('after')}\n"
                          f"endpoint: {res.get('endpoint')}\n"
                          f"confirmed: {res.get('confirmed')}")
        return _text(res)

    init_image = (args.get("init_image") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    missing = [k for k, v in (("init_image", init_image), ("prompt", prompt)) if not v]
    if missing:
        return _error("Missing required argument(s): " + ", ".join(missing))
    common = dict(
        negative_prompt=args.get("negative_prompt") or "",
        model=args.get("model"),
        steps=args.get("steps"),
        sampler=args.get("sampler"),
        cfg_scale=args.get("cfg_scale"),
        seed=int(args.get("seed", -1)),
        channel=args.get("channel"),
        out_dir=args.get("output_dir"),
        filename_prefix=args.get("filename_prefix"),
        timeout=args.get("timeout_s"),
        verify=args.get("verify"),
        dry_run=bool(args.get("dry_run")),
        strength=args.get("strength"),
    )
    try:
        if name == "edit_image":
            res = editor.edit_image(
                init_image, prompt, cfg,
                mask_image=args.get("mask_image"),
                regions=args.get("regions"),
                mask_blur=args.get("mask_blur"),
                inpaint_padding=args.get("inpaint_padding"),
                count=int(args.get("count", 1)),
                **common)
        else:
            res = editor.redraw_image(init_image, prompt, cfg,
                                      count=int(args.get("count", 1)), **common)
    except editor.EditError as exc:
        return _error(str(exc), exc.hint)
    except Exception as exc:  # pragma: no cover
        log(traceback.format_exc())
        return _error(f"{type(exc).__name__}: {exc}")

    if res.get("dry_run"):
        res["summary"] = ("dry run ok\nsize: %sx%s\nmask: %s" %
                          (res["size"][0], res["size"][1],
                           json.dumps(res.get("mask"), ensure_ascii=False)))
    else:
        res["summary"] = _summary_edit(res)
    return _text(res)


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
                "Local offline image, video and lip-sync generation via LocModel. Pick the tool "
                "that matches the sub-panel the user means, they are NOT interchangeable: "
                "generate_image / edit_image / redraw_image for the 画图 tab; generate_video "
                "(text- or image-to-video, webm/webp/avi) for 视频 → 生成视频; lipsync_generate "
                "(portrait + audio -> mp4 whose mouth follows the song) for 视频 → 对口型视频. "
                "Lip sync needs weights the user installs inside LocModel; without them the job "
                "fails outright, so call lipsync_status before promising anything. "
                "generate_video is asynchronous: call it with wait_seconds=0, then poll job_status. "
                "Call engine_doctor first if image generation fails."
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
