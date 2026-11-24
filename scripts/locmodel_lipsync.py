#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LocModel lipsync (``视频`` tab -> ``对口型视频`` sub-panel) bridge  (v3.3.0)

LocModel's talking / singing head video is NOT served by the image engine
(sd-server / ``/sdcpp/v1/*``).  It is a separate pipeline that LocModel ships as
a self-contained resource bundle and drives through three Tauri commands:

    lipsync_set_variant   pick the model variant (high quality / memory saver / auto)
    lipsync_setup         download models + a dedicated Python 3.12 venv (one-off)
    lipsync_generate      run one job

``lipsync_generate`` does exactly this, and this module reproduces it so an MCP
host / agent can drive the *same* models and the *same* runtime the GUI uses:

    1. resolve the variant   -> which model files must exist
    2. build ``lipsync/jobs/<job>/job.json``
       (image, audio, prompt, res, steps, accel, crop, seed, ...)
    3. run ``<lipsync venv>/python.exe lipsync/runner.py job.json``
    4. parse the runner's ``@@LM {...}`` progress lines and collect the mp4

The pipeline itself (see the runner LocModel writes to ``lipsync/runner.py``):

    InfiniteTalk (MeiGen-AI, Apache-2.0) on top of Wan2.1-I2V-14B-480P
      vocals      Demucs hybrid-transformer splits the vocal track
      audio       chinese-wav2vec2-base -> per-frame (25 fps) lip embedding
      text        umt5-xxl (fp8) T5 encoder
      picture     Wan2.1 VAE + open-clip xlm-roberta-large ViT-H/14
      diffusion   Wan2.1-I2V-14B-480P (+ InfiniteTalk audio cross-attention),
                  optional lightx2v distill LoRA for 4-step sampling
      mux         original song + generated frames -> mp4

HARD PRECONDITION -- read this before calling :func:`generate`
---------------------------------------------------------------
The models and the Python environment are **not** part of this MCP package and
are **not** installed by it.  They must be mounted once inside LocModel
(``视频`` -> ``对口型视频`` -> ``一键准备``).  If they are missing, the job cannot
run at all: there is no fallback, no degraded mode, no cloud fallback.  An agent
calling :func:`generate` without them gets a hard failure -- by design, because
silently "succeeding" here would mean shipping a broken video pipeline.

Environment overrides (also accepted from ``config.json`` under ``lipsync``)
--------------------------------------------------------------------------
``LOCMODEL_DATA``             LocModel program-data dir (holds ``settings.json``)
``LOCMODEL_LIPSYNC_PYTHON``   python.exe of the lipsync venv
``LOCMODEL_LIPSYNC_ROOT``     program side root (``runner.py`` / ``jobs/`` / venv / code)
``LOCMODEL_LIPSYNC_MODELS``   model side root (``dit/`` ``base/`` ``wav2vec/`` ...)
``LOCMODEL_LIPSYNC_VARIANT``  ``high`` | ``small`` | ``auto``
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERSION = "4.0.0"

# ---------------------------------------------------------------------------
# What the LocModel bundle contains, relative to the lipsync model root.
# Names come from the LocModel 3.6.1 binary; the scanner is pattern based
# because LocModel may add quants between releases.
# ---------------------------------------------------------------------------

VARIANTS = {
    "high": {
        "label": "高质量版 (high quality)",
        "dit": "Wan2.1-I2V-14B-480P fp8 / bf16 weights",
        "infinitetalk": "InfiniteTalk single fp8 weights",
        "approx_gb": 25.1,
        "min_ram_gb": 48,
        "min_vram_gb": 8,
        # 实测（LocModel.exe 3.6.1 字符串表）：high 档实际下载的文件名
        "files": [
            "dit/Wan2_1-I2V-14B-480P_fp8_e4m3fn.safetensors",
            "single/infinitetalk.safetensors",
            "lora/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors",
            "base/Wan2.1_VAE.pth",
            "base/models_clip_open-clip-xlm-roberta-large-vit-huge-14.pth",
            "quant_models/t5_fp8.safetensors（+ t5_map_fp8.json）",
            "base/google/umt5-xxl/*      （tokenizer）",
            "base/xlm-roberta-large/*    （tokenizer）",
            "wav2vec/*                  （TencentGameMate/chinese-wav2vec2-base）",
            "demucs/*.th                （hybrid_transformer）",
        ],
    },
    "small": {
        "label": "省内存版 (memory saver)",
        "dit": "Wan2.1-I2V-14B-480P Q4_K_M GGUF",
        "infinitetalk": "InfiniteTalk Single Q4_K_M GGUF",
        "approx_gb": 11.9,
        "min_ram_gb": 16,
        "min_vram_gb": 8,
        # 实测（LocModel.exe 3.6.1 字符串表）：small 档走 GGUF 量化，连文本编码器也是 GGUF
        "files": [
            "dit/wan2.1-i2v-14b-480p-Q4_K_M.gguf",
            "single/Wan2_1-InfiniteTalk_Single_Q4_K_M.gguf",
            "lora/lightx2v_I2V_14B_480p_rank64_bf16.safetensors",
            "split_files/vae/wan_2.1_vae.safetensors",
            "models_clip_open-clip-xlm-roberta-large-vit-huge-14.pth",
            "umt5-xxl-encoder-Q8_0.gguf  （GGUF 文本编码器，省内存档不下载 t5_fp8）",
            "base/google/umt5-xxl/*      （tokenizer）",
            "base/xlm-roberta-large/*     （tokenizer）",
            "wav2vec/*                  （TencentGameMate/chinese-wav2vec2-base）",
            "demucs/*.th                （hybrid_transformer）",
        ],
    },
}

RES_BUCKETS = ("480", "720", "custom")

# ---------------------------------------------------------------------------
# Measured facts about LocModel's built-in "对口型视频" sub-panel (GUI: 视频 -> 对口型视频).
#
# Evidence: LocModel.exe 3.6.1 string table + the embedded ``lipsync/runner.py`` source
# that the GUI writes to disk.  It is a *separate* pipeline from the sd-server
# ``vid_gen`` API: it never touches port 8686 and has its own python env.
# ---------------------------------------------------------------------------

PIPELINE_FACTS = {
    "gui_path": "视频选项卡 → 对口型视频（独立子界面：上传人物图片/视频 + 一首歌）",
    "evidence": "LocModel.exe 3.6.1 字符串表 + GUI 写出到 lipsync/runner.py 的内嵌源码；"
                "非 sd-server 路径，未实测推理（本机未挂载模型）",
    "host_commands": ["lipsync_set_variant（选 high/small/auto）", "lipsync_setup（一键准备）",
                      "lipsync_generate（开始生成）", "lipsync_cancel（取消）",
                      "进度事件 lipsync-progress；前端 webview lipsync.js，后端 src/lipsync.rs"],
    "engine": "MeiGen-AI InfiniteTalk (Apache-2.0), 单人口型驱动",
    "runner": "程序数据目录/lipsync/runner.py（由 LocModel 写出，可用内置副本恢复）",
    "isolated_from": "sd-server / vid_gen API（8686 端口）完全独立，不能走 generate_video",
    "models": {
        "dit": "Wan2.1-I2V-14B-480P —— high=fp8(约17GB) / small=Q4_K_M GGUF",
        "infinitetalk": "InfiniteTalk Single（音频交叉注意力权重）—— high=fp8 / small=Q4_K_M GGUF",
        "lora": "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16（4 步加速，推理时叠加）",
        "t5": "高质量版 t5_fp8.safetensors + t5_map_fp8.json（optimum-quanto fp8）；"
              "省内存版 umt5-xxl-encoder-Q8_0.gguf（GGUF 文本编码）",
        "umt5": "umt5-xxl tokenizer（base/google/umt5-xxl）",
        "clip": "open-clip ViT-H/14 + xlm-roberta-large（图像条件）",
        "vae": "Wan2.1_VAE.pth",
        "wav2vec": "chinese-wav2vec2-base（音频特征，wav2vec/）",
        "demucs": "Demucs hybrid-transformer 955717e8-8726e21a.th（人声分离）",
    },
    "downloads": "一键准备：模型约 24.2 GB + 运行环境约 6 GB（CUDA 版 PyTorch），只需一次，断点续传",
    "env": {
        "python": "独立 venv（Python 3.12，uv 创建），非 LocModel 本体环境",
        "key_packages": ["torch(CUDA)", "triton-windows<3.4", "sageattention==1.0.6",
                         "diffusers==0.33.1", "transformers==4.51.3", "accelerate==1.6.0",
                         "optimum-quanto==0.2.6", "gguf==0.17.1", "demucs==4.0.1",
                         "openunmix==1.3.0", "numpy==1.26.4"],
        "env_vars": ["TRITON_CACHE_DIR=<lipsync>/triton-cache", "HF_HUB_OFFLINE=1",
                     "TRANSFORMERS_OFFLINE=1", "PYTHONUTF8=1"],
        "hardware": "必须 NVIDIA CUDA；显存 ≥8 GB；small 版建议 ≥16 GB 内存，high 版 ≥48 GB",
    },
    "output": "LocModel_<时间戳>_lipsync.mp4（libx264 crf17 / 25 fps，音轨用原曲）",
}

DEFAULT_NEGATIVE = (
    "bright tones, overexposed, static, blurred details, subtitles, style, works, paintings, "
    "images, static, overall gray, worst quality, low quality, JPEG compression residue, ugly, "
    "incomplete, extra fingers, poorly drawn hands, poorly drawn faces, deformed, disfigured, "
    "misshapen limbs, fused fingers, still picture, messy background, three legs, many people in "
    "the background, walking backwards"
)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".mp4", ".mkv"}

MOUNT_HINT = (
    "对口型视频的专用模型和运行环境还没在 LocModel 上挂载。打开 LocModel →「视频」选项卡 →"
    "「对口型视频」子界面 → 点「一键准备」（下载模型约 24.2 GB + 运行环境约 6 GB，只需一次），"
    "装完再调用本工具。未挂载专用模型会导致整体任务失败，本工具不会降级、不会改走其它引擎。"
)

RUN_ENV = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "TOKENIZERS_PARALLELISM": "false",
}


class LipsyncError(RuntimeError):
    """User-facing failure with an actionable hint."""

    def __init__(self, message: str, code: str = "lipsync_error", hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


# ---------------------------------------------------------------------------
# Locating LocModel's own installation
# ---------------------------------------------------------------------------

def _candidate_data_roots(cfg: dict | None = None) -> list[Path]:
    cfg = cfg or {}
    roots: list[Path] = []
    for raw in (
        os.environ.get("LOCMODEL_DATA"),
        cfg.get("data_dir"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "LocModel"),
        os.path.join(os.environ.get("APPDATA", ""), "LocModel"),
        r"C:\Users\Public\LocModel",
    ):
        if raw:
            roots.append(Path(raw))
    seen: list[Path] = []
    for r in roots:
        if r not in seen:
            seen.append(r)
    return seen


def data_root(cfg: dict | None = None) -> Path:
    """LocModel program-data dir (the one holding settings.json / lipsync/)."""
    for root in _candidate_data_roots(cfg):
        if (root / "settings.json").exists() or (root / "lipsync").is_dir():
            return root
    roots = _candidate_data_roots(cfg)
    return roots[0] if roots else Path.home() / "LocModel"


def locmodel_settings(cfg: dict | None = None) -> dict:
    path = data_root(cfg) / "settings.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def lipsync_root(cfg: dict | None = None) -> Path:
    """Program-side root: holds ``runner.py``, ``runner.dev.py``, ``jobs/``, venv,
    InfiniteTalk code and ``perf.log`` / ``triton-cache``.  On a stock install this is
    ``%LOCALAPPDATA%\\LocModel\\lipsync``.
    """
    cfg = cfg or {}
    env = os.environ.get("LOCMODEL_LIPSYNC_ROOT") or cfg.get("root")
    if env:
        return Path(env)
    return data_root(cfg) / "lipsync"


def models_root(cfg: dict | None = None, *, override: str | None = None) -> Path:
    """Model-side root: ``dit/`` ``lora/`` ``base/`` ``wav2vec/`` ``quant_models|t5/``.

    LocModel downloads these next to the user's model library, i.e.
    ``<settings.models_dir>\\lipsync`` (the "模型目录" shown in the GUI), which is a
    *different* folder from the program root.  Falls back to the program root when
    no library path is configured or the folder does not exist yet.
    """
    cfg = cfg or {}
    if override:
        return Path(override)
    env = os.environ.get("LOCMODEL_LIPSYNC_MODELS") or cfg.get("models_root")
    if env:
        return Path(env)
    settings = locmodel_settings(cfg)
    models_dir = str(settings.get("models_dir") or "").strip()
    if models_dir:
        cand = Path(models_dir) / "lipsync"
        if cand.is_dir():
            return cand
    if models_dir:
        # library path configured but the lipsync subtree is not created yet: report the
        # expected location so status() can tell where the download will land
        return Path(models_dir) / "lipsync"
    prog = lipsync_root(cfg)
    for sub in (prog / "models", prog):
        if sub.is_dir():
            return sub
    return prog


def jobs_root(cfg: dict | None = None) -> Path:
    return lipsync_root(cfg) / "jobs"


def runner_path(cfg: dict | None = None) -> Path:
    return lipsync_root(cfg) / "runner.py"


# ---------------------------------------------------------------------------
# Variant + hardware
# ---------------------------------------------------------------------------

_RAM_ERROR: list[str] = []


def _ram_gb() -> float | None:
    _RAM_ERROR.clear()
    try:
        import psutil  # type: ignore

        return round(psutil.virtual_memory().total / 2 ** 30, 1)
    except Exception as exc:
        _RAM_ERROR.append(f"psutil: {type(exc).__name__}: {exc}")
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return round(st.ullTotalPhys / 2 ** 30, 1)
            _RAM_ERROR.append("GlobalMemoryStatusEx returned 0")
    except Exception as exc:
        _RAM_ERROR.append(f"ctypes: {type(exc).__name__}: {exc}")
    return _ram_gb_powershell()


def _ram_gb_powershell() -> float | None:
    """Last resort (frozen exe where ctypes/psutil are unavailable)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
            capture_output=True, text=True, timeout=15,
        )
        raw = (out.stdout or "").strip().splitlines()
        if raw:
            return round(float(raw[-1].strip()) / 2 ** 30, 1)
    except Exception as exc:
        _RAM_ERROR.append(f"powershell: {type(exc).__name__}: {exc}")
    return None


def hardware() -> dict:
    """GPU name / VRAM via nvidia-smi, system RAM via psutil."""
    info: dict = {"gpu": None, "vram_gb": None, "ram_gb": _ram_gb(), "nvidia_smi": False}
    if info["ram_gb"] is None:
        info["ram_error"] = list(_RAM_ERROR)
    exe = shutil.which("nvidia-smi")
    if not exe:
        return info
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        line = (out.stdout or "").strip().splitlines()
        if line:
            parts = [p.strip() for p in line[0].split(",")]
            info["gpu"] = parts[0]
            if len(parts) > 1:
                try:
                    info["vram_gb"] = round(float(parts[1]) / 1024, 1)
                except ValueError:
                    pass
            info["nvidia_smi"] = True
    except Exception:
        pass
    return info


def resolve_variant(cfg: dict | None = None) -> dict:
    """``auto`` follows LocModel: high quality when RAM >= 48 GB, else memory saver."""
    cfg = cfg or {}
    hw = hardware()
    want = (
        os.environ.get("LOCMODEL_LIPSYNC_VARIANT")
        or cfg.get("variant")
        or locmodel_settings(cfg).get("lipsync_variant")
        or "auto"
    )
    want = str(want).strip().lower()
    if want in ("high", "quality", "高质量版"):
        return {"variant": "high", "requested": "high", "reason": "explicitly set to high quality"}
    if want in ("small", "lite", "省内存版"):
        return {"variant": "small", "requested": "small", "reason": "explicitly set to memory saver"}
    ram = hw.get("ram_gb")
    if ram is not None:
        if ram >= VARIANTS["high"]["min_ram_gb"]:
            return {"variant": "high", "requested": "auto",
                    "reason": f"auto: {ram} GB RAM >= {VARIANTS['high']['min_ram_gb']} GB "
                              f"-> high quality"}
        return {"variant": "small", "requested": "auto",
                "reason": f"auto: {ram} GB RAM < {VARIANTS['high']['min_ram_gb']} GB "
                          f"-> memory saver"}
    return {"variant": "small", "requested": "auto", "reason": "auto: RAM unknown -> memory saver"}


# ---------------------------------------------------------------------------
# Model / runtime scan
# ---------------------------------------------------------------------------

def _iter_files(root: Path, limit: int = 20000):
    if not root.exists():
        return
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "venv", ".venv", "__pycache__")]
        for name in filenames:
            n += 1
            if n > limit:
                return
            yield Path(dirpath) / name


def _pick(files: list[Path], patterns: list[str], exclude: list[str] | None = None) -> Path | None:
    exclude = exclude or []
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for f in files:
            text = f.as_posix()
            if any(re.search(ex, text, re.I) for ex in exclude):
                continue
            if rx.search(text):
                return f
    return None


_SCAN_CACHE: dict = {}


def scan(cfg: dict | None = None, *, refresh: bool = True,
         models_dir: str | None = None) -> dict:
    """Find every asset the runner needs, without hard-coding release filenames.

    两个真实档位的目录布局不同，扫描必须同时兼容：

    * 高质量档：``base/Wan2.1_VAE.pth``、``base/config.json``、
      ``quant_models/t5_fp8.safetensors`` + ``t5_map_fp8.json``
    * 省内存档：``split_files/vae/wan_2.1_vae.safetensors``（**小写、不在 base 下**）、
      ``umt5-xxl-encoder-Q8_0.gguf``（GGUF 文本编码器，**没有** t5_fp8/t5_map）

    所以 tokenizer 目录按路径独立探测，``base`` 只作为 runner 拼相对路径的锚点，
    且允许回退（config.json -> tokenizer 祖先 -> VAE 父目录）。
    """
    cfg = cfg or {}
    root = models_root(cfg, override=models_dir)
    key = str(root)
    found: dict = {"root": str(root), "exists": root.is_dir(), "files": 0}
    if not found["exists"]:
        return found
    cached = None if refresh else _SCAN_CACHE.get(key)
    if cached is not None:
        return dict(cached)
    files = list(_iter_files(root))
    found["files"] = len(files)

    base_cfg = _pick(files, [r"/base/config\.json$", r"/config\.json$"])
    base = base_cfg.parent if base_cfg else None

    vae_file = _pick(files, [r"/Wan2\.1_VAE\.pth$", r"/Wan.*VAE.*\.(pth|safetensors)$",
                             r"/wan.*vae.*\.safetensors$"])
    found["vae"] = str(vae_file) if vae_file else None
    clip_file = _pick(files, [r"models_clip_open-clip.*\.pth$", r"open-clip.*\.pth$"])
    found["clip"] = str(clip_file) if clip_file else None

    # tokenizer 目录：不依赖 base，直接按路径特征找
    found["umt5"] = None
    for cand in ([base / "google" / "umt5-xxl", base / "umt5-xxl"] if base else []):
        if cand.is_dir():
            found["umt5"] = str(cand)
            break
    if not found["umt5"]:
        tok = _pick(files, [r"/google/umt5-xxl/[^/]+$", r"/umt5-xxl/[^/]+$"])
        if tok:
            found["umt5"] = str(tok.parent)

    found["xlm_roberta"] = None
    for cand in ([base / "xlm-roberta-large", base / "xlm_roberta_large"] if base else []):
        if cand.is_dir():
            found["xlm_roberta"] = str(cand)
            break
    if not found["xlm_roberta"]:
        tok = _pick(files, [r"/xlm-roberta-large/[^/]+$", r"/xlm_roberta_large/[^/]+$"])
        if tok:
            found["xlm_roberta"] = str(tok.parent)

    # base 只是 runner 的锚点：优先 config.json，其次 tokenizer 的 base/ 祖先，最后 VAE 父目录
    if base is None and found.get("umt5"):
        parts = Path(found["umt5"]).parts
        if len(parts) >= 3 and parts[-2] == "google":
            base = Path(*parts[:-3]) if len(parts) >= 4 else None
    if base is None and vae_file is not None:
        base = vae_file.parent
    found["base"] = str(base) if base else None

    t5_map = _pick(files, [r"t5_map_fp8\.json$"])
    # 高质量档是 t5_fp8.safetensors（optimum-quanto fp8），省内存档换成 umt5-xxl-encoder-Q8_0.gguf，
    # 两种都算"文本编码器就位"。
    t5_file = _pick(files, [r"t5_fp8\.safetensors$", r"t5.*\.safetensors$",
                            r"umt5-xxl-encoder[^/]*\.gguf$", r"umt5.*encoder.*\.gguf$"])
    found["t5"] = str(t5_file) if t5_file else None
    found["t5_map"] = str(t5_map) if t5_map else None
    found["t5_dir"] = str((t5_map or t5_file).parent) if (t5_map or t5_file) else None

    w2v = _pick(files, [r"/wav2vec/preprocessor_config\.json$", r"/wav2vec/config\.json$",
                        r"chinese-wav2vec2.*config\.json$"])
    found["wav2vec"] = str(w2v.parent) if w2v else None

    demucs_file = _pick(files, [r"\.th$"])
    found["demucs"] = str(demucs_file) if demucs_file else None
    lora_file = _pick(files, [r"lightx2v.*\.safetensors$"])
    found["lora"] = str(lora_file) if lora_file else None
    infinitetalk = _pick(files, [r"infinitetalk.*\.(safetensors|gguf)$"])
    found["infinitetalk"] = str(infinitetalk) if infinitetalk else None
    found["dit"] = str(_pick(
        files,
        [r"Wan2.*I2V.*14B.*480P.*\.(safetensors|gguf)$",
         r"Wan2.*I2V.*\.(safetensors|gguf)$",
         r"/dit/.*\.(safetensors|gguf)$",
         r"4b-480p.*\.gguf$"],
        exclude=[r"infinitetalk", r"lightx2v", r"lora", r"t5"],
    )) or None
    _SCAN_CACHE[key] = dict(found)
    return found


def _python_candidates(cfg: dict | None = None) -> list[Path]:
    cfg = cfg or {}
    root = lipsync_root(cfg)
    env = os.environ.get("LOCMODEL_LIPSYNC_PYTHON") or cfg.get("python")
    cands: list[Path] = [Path(env)] if env else []
    for sub in ("venv", ".venv", "env", "python", "runtime"):
        for tail in (("Scripts", "python.exe"), ("bin", "python3"), ("bin", "python")):
            cands.append(root / sub / Path(*tail))
    return cands


def find_python(cfg: dict | None = None) -> Path | None:
    for cand in _python_candidates(cfg):
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    # Fallback: LocModel builds the venv with uv and the folder name is not contractual
    root = lipsync_root(cfg)
    if root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(str(root)):].count(os.sep)
            if depth > 3:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in ("models", "dit", "base", "lora",
                                                            "jobs", "triton-cache", "__pycache__")]
            if "python.exe" in filenames:
                return Path(dirpath) / "python.exe"
            if "python3" in filenames:
                return Path(dirpath) / "python3"
    return None


def _code_dirs(cfg: dict | None = None) -> list[Path]:
    root = lipsync_root(cfg or {})
    out = [root / "code", root / "InfiniteTalk"]
    for f in _iter_files(root, limit=4000):
        if f.name == "attention.py" and f.parent.name == "modules" and f.parent.parent.name == "wan":
            out.append(f.parent.parent.parent)
    return out


def find_code_dir(cfg: dict | None = None) -> Path | None:
    for cand in _code_dirs(cfg):
        try:
            if (cand / "wan" / "modules" / "attention.py").is_file():
                return cand
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Readiness report / gate
# ---------------------------------------------------------------------------

REQUIRED_KEYS = ("base", "vae", "clip", "umt5", "xlm_roberta", "t5", "wav2vec",
                 "demucs", "dit", "infinitetalk")
REQUIRED_LABELS = {
    "base": "Wan2.1 base/config.json",
    "vae": "Wan2.1_VAE.pth",
    "clip": "open-clip xlm-roberta-large ViT-H/14",
    "umt5": "umt5-xxl tokenizer folder",
    "xlm_roberta": "xlm-roberta-large tokenizer folder",
    "t5": "文本编码器（高质量版 t5_fp8.safetensors / 省内存版 umt5-xxl-encoder-Q8_0.gguf）",
    "t5_map": "t5_map_fp8.json",
    "wav2vec": "chinese-wav2vec2-base (wav2vec/)",
    "demucs": "Demucs hybrid-transformer (*.th)",
    "dit": "Wan2.1-I2V-14B-480P diffusion weights",
    "infinitetalk": "InfiniteTalk weights",
}


def status(cfg: dict | None = None, *, refresh: bool = True,
           models_dir: str | None = None) -> dict:
    """Everything an agent needs before calling generate().

    ``refresh``     re-scan the model folder (a cached snapshot is reused when False)
    ``models_dir``  override the lipsync model root for this call only
    """
    cfg = dict(cfg or {})
    if models_dir:
        cfg["models_root"] = models_dir
    var = resolve_variant(cfg)
    models = scan(cfg, refresh=refresh, models_dir=models_dir)
    py = find_python(cfg)
    code = find_code_dir(cfg)
    runner = runner_path(cfg)

    missing = [{"key": k, "need": REQUIRED_LABELS[k], "variant_hint": VARIANTS[var["variant"]].get(k)}
               for k in REQUIRED_KEYS if not models.get(k)]
    notes: list[str] = []
    if models.get("t5") and not models.get("t5_map") and models["t5"].lower().endswith(".safetensors"):
        notes.append("t5_map_fp8.json 未找到：fp8 文本编码器可能缺少量化映射表，"
                     "如加载报错请在「一键准备」里补全（省内存版用 GGUF 编码器，不需要该文件）")
    if not models.get("lora"):
        notes.append("lightx2v 加速 LoRA 未找到：仍可生成，但无法 4 步加速，速度会明显变慢")

    hw = hardware()
    if not hw.get("nvidia_smi"):
        notes.append("nvidia-smi 不可用：对口型视频必须用 NVIDIA 显卡（CUDA），否则任务会直接失败")
    elif hw.get("vram_gb") and hw["vram_gb"] < 8:
        notes.append(f"显存只有 {hw['vram_gb']} GB，低于最低要求 8 GB")
    ram = hw.get("ram_gb")
    if ram is not None and ram < VARIANTS[var["variant"]]["min_ram_gb"]:
        notes.append(f"内存 {ram} GB 低于{var['variant']}版建议的 "
                     f"{VARIANTS[var['variant']]['min_ram_gb']} GB，生成会很慢或失败")

    ready = not missing and bool(py) and bool(code) and bool(runner.exists())
    if not py:
        notes.append("找不到对口型专用 Python 环境（lipsync/venv/Scripts/python.exe）："
                     "需要在「视频 → 对口型视频 → 一键准备」里准备运行环境")
    if not code:
        notes.append("找不到 InfiniteTalk 源码目录（lipsync/code）：需要「一键准备」下载程序包")
    if not runner.exists():
        notes.append("lipsync/runner.py 尚未生成（LocModel 首次点开始生成时才写出）："
                     "本工具可用内置副本自动写出，无需手动处理")

    report = {
        "ok": ready,
        "version": VERSION,
        "sub_panel": "视频选项卡 → 对口型视频（上传人物图片/视频 + 音频）",
        "pipeline": "InfiniteTalk + Wan2.1-I2V-14B-480P (fp8/GGUF) | Demucs + chinese-wav2vec2 "
                    "+ umt5-xxl (fp8 或 Q8_0 GGUF) + Wan2.1 VAE + open-clip ViT-H/14",
        "pipeline_facts": PIPELINE_FACTS,
        "variant": var,
        "paths": {
            "data_root": str(data_root(cfg)),
            "lipsync_root": str(lipsync_root(cfg)),
            "models_root": models.get("root"),
            "jobs_root": str(jobs_root(cfg)),
            "runner": str(runner),
            "python": str(py) if py else None,
            "code_dir": str(code) if code else None,
        },
        "models": {k: models.get(k) for k in REQUIRED_KEYS + ("lora", "t5_dir")},
        "missing": missing,
        "hardware": hw,
        "notes": notes,
        "mount_required": True,
        "mount_hint": MOUNT_HINT,
    }
    if not ready:
        report["blocking"] = (
            "未在 LocModel 挂载对口型视频专用模型 / 运行环境，任务无法开始；"
            "若强行调用将整体失败（本工具不会降级为其它引擎）。"
        )
    return report


def require_ready(cfg: dict | None = None) -> dict:
    rep = status(cfg)
    if rep["ok"]:
        return rep
    need = "；".join(m["need"] for m in rep["missing"]) or "运行环境"
    raise LipsyncError(
        f"对口型视频未就绪，缺少：{need}。未在 LocModel 挂载专用模型将导致整体任务失败。",
        code="lipsync_not_mounted",
        hint=MOUNT_HINT,
    )


# ---------------------------------------------------------------------------
# Job assembly + execution
# ---------------------------------------------------------------------------

RUNNER_EMBED = "lipsync_runner_embed.py"


def ensure_runner(cfg: dict | None = None) -> Path:
    """LocModel only writes runner.py on the first GUI run; restore it from our copy."""
    target = runner_path(cfg)
    if target.exists():
        return target
    embed = Path(__file__).resolve().parent / RUNNER_EMBED
    if not embed.is_file():
        raise LipsyncError(
            "lipsync/runner.py 不存在，内置副本也缺失，无法运行对口型视频。",
            code="runner_missing",
            hint="请在 LocModel「视频 → 对口型视频」里点一次「开始生成」，让 LocModel 写出 runner.py。",
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(embed, target)
    except Exception as exc:
        raise LipsyncError(f"写出 runner.py 失败：{exc}", code="runner_write_failed",
                           hint=MOUNT_HINT) from exc
    return target


def _check_media(path: str, kinds: set[str], what: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_file():
        raise LipsyncError(f"找不到{what}：{path}", code="file_missing",
                           hint="请传入本机存在的绝对路径。")
    if p.suffix.lower() not in kinds:
        raise LipsyncError(
            f"{what}格式不支持：{p.suffix}", code="bad_media",
            hint=f"支持的扩展名：{', '.join(sorted(kinds))}",
        )
    return p


def norm_crop(crop) -> dict | None:
    """Normalise the crop argument into the runner's ``{x, y, w, h}`` (0..1) form.

    ``None`` / ``"full"``   whole picture (LocModel default)
    ``"head"``              upper-middle square, the usual talking-head framing
    ``"upper"``             upper half, keeps shoulders in frame
    ``{x, y, w, h}``        explicit normalised box
    """
    if crop in (None, "", "full", "none", "original"):
        return None
    if isinstance(crop, dict):
        try:
            box = {k: float(crop[k]) for k in ("x", "y", "w", "h")}
        except KeyError as exc:
            raise LipsyncError(f"crop 缺少字段 {exc}", code="bad_crop",
                               hint="crop 需要 {x, y, w, h}，取值 0~1 的归一化比例。") from exc
        for k, v in box.items():
            if not 0.0 <= v <= 1.0:
                raise LipsyncError(f"crop.{k}={v} 超出 0~1 范围", code="bad_crop",
                                   hint="四个值都是相对原图的 0~1 比例。")
        if box["x"] + box["w"] > 1.01 or box["y"] + box["h"] > 1.01:
            raise LipsyncError("crop 的 x+w / y+h 超出图片范围", code="bad_crop")
        return box
    key = str(crop).strip().lower()
    if key == "head":
        return {"x": 0.15, "y": 0.0, "w": 0.7, "h": 0.7}
    if key == "upper":
        return {"x": 0.0, "y": 0.0, "w": 1.0, "h": 0.55}
    raise LipsyncError(f"不支持的 crop={crop!r}", code="bad_crop",
                       hint="可用 full / head / upper，或 {x, y, w, h} 归一化字典。")


def build_job(image: str, audio: str, *, prompt: str = "", negative: str = "", res: str = "480",
              width=None, height=None, crop=None, steps=None, accel: bool = True, seed: int = -1,
              max_seconds: float = 0, color_fix: float = 1.0, vae_tiled: bool = False,
              sage: bool = True, crf: int = 18, vocals: str = "auto",
              out_path: Path | None = None, work_dir: Path | None = None) -> dict:
    cfg = _LOADED.get("cfg") or {}
    rep = require_ready(cfg)
    models = rep["models"]
    img = _check_media(image, IMAGE_EXT | VIDEO_EXT, "人物图片/视频")
    aud = _check_media(audio, AUDIO_EXT, "音频")
    if res not in RES_BUCKETS:
        raise LipsyncError(f"res 只能是 {'/'.join(RES_BUCKETS)}，收到 {res!r}", code="bad_res",
                           hint="480 = 480P 长边自适应；720 = 720P；custom 需要同时给 width/height")
    if res == "custom" and not (width and height):
        raise LipsyncError("res=custom 时需要同时提供 width 和 height", code="bad_res",
                           hint="宽高会被对齐到 16 的倍数，范围 256..1920")
    code_dir = find_code_dir(cfg)
    job = {
        "image": str(img),
        "audio": str(aud),
        "prompt": prompt or "a person is singing and speaking to the camera, natural head motion",
        "negative": negative or DEFAULT_NEGATIVE,
        "res": res,
        "accel": bool(accel),
        "sage": bool(sage),
        "vae_tiled": bool(vae_tiled),
        "color_fix": color_fix,
        "crf": int(crf),
        "vocals": "auto" if str(vocals).lower() in ("auto", "demucs", "split") else "none",
        "work_dir": str(work_dir),
        "out": str(out_path),
        "code_dir": str(code_dir),
        "models": {
            "base": models["base"],
            "t5_dir": models["t5_dir"],
            "dit": models["dit"],
            "infinitetalk": models["infinitetalk"],
            "wav2vec": models["wav2vec"],
            "demucs": models["demucs"],
        },
    }
    if models.get("lora"):
        job["models"]["lora"] = models["lora"]
    if res == "custom":
        w, h = int(width), int(height)
        if not (256 <= w <= 1920 and 256 <= h <= 1920):
            raise LipsyncError(f"自定义宽高需在 256..1920，收到 {w}×{h}", code="bad_res",
                               hint="建议 832×480 或更小（8 GB 显存）。")
        job["width"], job["height"] = w, h
    box = norm_crop(crop)
    if box:
        job["crop"] = box
    if steps:
        job["steps"] = int(steps)
    if seed is not None and seed >= 0:
        job["seed"] = int(seed)
    if max_seconds:
        job["max_seconds"] = float(max_seconds)
    return job


_LOADED: dict = {}


def _out_path(output_dir: str | None, cfg: dict) -> Path:
    directory = Path(output_dir).expanduser() if output_dir else None
    if directory is None:
        configured = (cfg or {}).get("output_dir") or os.environ.get("LOCMODEL_LIPSYNC_OUT")
        directory = Path(configured).expanduser() if configured else Path.home() / "Pictures" / "LocModel"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return directory / f"LocModel_{stamp}_lipsync.mp4"


def generate(image: str, audio: str, cfg: dict | None = None, *, prompt: str = "",
             negative: str = "", res: str = "480", width=None, height=None, crop=None,
             steps=None, accel: bool = True, seed: int = -1, max_seconds: float = 0,
             vocals: str = "auto", color_fix: float = 1.0, crf: int = 18,
             vae_tiled: bool = False, sage: bool = True,
             output_dir: str | None = None, timeout_s: int = 0,
             on_progress=None, dry_run: bool = False) -> dict:
    """Run one lipsync job the same way LocModel's lipsync_generate does."""
    cfg = cfg or {}
    _LOADED["cfg"] = cfg
    rep = require_ready(cfg)
    python = Path(rep["paths"]["python"])
    runner = ensure_runner(cfg)
    out = _out_path(output_dir, cfg)
    work = jobs_root(cfg) / f"job-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    job = build_job(image, audio, prompt=prompt, negative=negative, res=res, width=width,
                    height=height, crop=crop, steps=steps, accel=accel, seed=seed,
                    max_seconds=max_seconds, vocals=vocals, color_fix=color_fix, crf=crf,
                    vae_tiled=vae_tiled, sage=sage, out_path=out, work_dir=work)
    job_path = work / "job.json"
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "ok": False, "job_json": str(job_path), "work_dir": str(work), "output": str(out),
        "variant": rep["variant"]["variant"], "res": res, "steps": job.get("steps"),
        "accel": accel, "dry_run": bool(dry_run), "stages": [],
    }
    if dry_run:
        result.update(ok=True, message="dry run：检查通过，未启动推理")
        return result

    env = os.environ.copy()
    env.update(RUN_ENV)
    cache = lipsync_root(cfg) / "triton-cache"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        env["TRITON_CACHE_DIR"] = str(cache)
    except OSError:
        pass
    t0 = time.time()
    proc = subprocess.Popen(
        [str(python), str(runner), str(job_path)],
        cwd=str(rep["paths"]["code_dir"]),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
    )
    last = ""
    try:
        for raw in proc.stdout:  # type: ignore[union-attr]
            line = raw.rstrip("\n")
            last = line or last
            if line.startswith("@@LM "):
                try:
                    msg = json.loads(line[5:])
                except Exception:
                    continue
                if "error" in msg:
                    raise LipsyncError(str(msg["error"]), code="lipsync_failed", hint=MOUNT_HINT)
                if "done" in msg:
                    result["output"] = msg["done"]
                if msg.get("stage"):
                    result["stages"].append({"stage": msg["stage"], "pct": msg.get("pct"),
                                             "msg": msg.get("msg")})
                if msg.get("info"):
                    result["info"] = msg["info"]
                if on_progress:
                    try:
                        on_progress(msg)
                    except Exception:
                        pass
            if timeout_s and time.time() - t0 > timeout_s:
                proc.kill()
                raise LipsyncError(f"超过 {timeout_s}s 仍未完成，已中止", code="timeout",
                                   hint="长歌在 8 GB 显卡上需要较长时间，可加大 timeout_s 或缩短音频。")
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    result["returncode"] = code
    result["elapsed_s"] = round(time.time() - t0, 1)
    final = Path(result["output"])
    if code != 0 or not final.exists():
        raise LipsyncError(
            f"对口型视频生成失败（退出码 {code}）：{(last or '')[:300]}",
            code="lipsync_failed",
            hint="常见原因：显存/内存不足（换 480P、关掉占显存的程序）、模型目录空间不够、"
                 "或显存中被画图引擎占满。日志见 lipsync/perf.log 与 job 目录。",
        )
    result.update(ok=True, size_mb=round(final.stat().st_size / 2 ** 20, 2))
    return result


# ---------------------------------------------------------------------------
# CLI (diagnostics / smoke tests) -- `python locmodel_lipsync.py status|run`
# ---------------------------------------------------------------------------

_CLI_USAGE = """LocModel 视频选项卡 → 对口型视频（独立管线，不走 sd-server）

用法:
  lipsync status                      就绪体检（JSON）：ok / variant / models / missing / paths
  lipsync run --image <图> --audio <音频> [--res 480|720|custom] [--output-dir <目录>]
  lipsync help                        本说明

硬约束：未在 LocModel 挂载专用模型与运行环境（模型约 24.2 GB + 环境约 6 GB）将导致整体任务失败，
本工具不会降级、不会改走 generate_video、不会调用云端。挂载入口：LocModel →「视频」→「对口型视频」→「一键准备」。
"""


def _cli(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd in ("help", "-h", "--help"):
        print(_CLI_USAGE)
        return 0
    if cmd == "status":
        print(json.dumps(status({}), ensure_ascii=False, indent=2))
        return 0
    if cmd == "run":
        import argparse

        ap = argparse.ArgumentParser(prog="locmodel_lipsync.py run")
        ap.add_argument("--image", required=True)
        ap.add_argument("--audio", required=True)
        ap.add_argument("--prompt", default="")
        ap.add_argument("--res", default="480", choices=list(RES_BUCKETS))
        ap.add_argument("--width", type=int)
        ap.add_argument("--height", type=int)
        ap.add_argument("--steps", type=int)
        ap.add_argument("--crop", default="full")
        ap.add_argument("--vocals", default="auto", choices=["auto", "none"])
        ap.add_argument("--seed", type=int, default=-1)
        ap.add_argument("--seconds", type=float, default=0)
        ap.add_argument("--output-dir")
        ap.add_argument("--timeout", type=int, default=0)
        ap.add_argument("--no-accel", action="store_true")
        ap.add_argument("--dry-run", action="store_true")
        args = ap.parse_args(argv[2:])
        try:
            res = generate(
                args.image, args.audio, {}, prompt=args.prompt, res=args.res, width=args.width,
                height=args.height, crop=args.crop, steps=args.steps, seed=args.seed,
                max_seconds=args.seconds, vocals=args.vocals,
                output_dir=args.output_dir, timeout_s=args.timeout, accel=not args.no_accel,
                dry_run=args.dry_run,
                on_progress=lambda m: print(f"[{m.get('stage')}] {m.get('msg') or ''}",
                                            file=sys.stderr),
            )
        except LipsyncError as exc:
            print(json.dumps({"ok": False, "error": str(exc), "hint": exc.hint},
                             ensure_ascii=False, indent=2))
            return 2
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv))
