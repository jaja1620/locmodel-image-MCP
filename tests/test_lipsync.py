#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tests for the lipsync (GUI 视频 → 对口型视频) pipeline bridge.

Nothing here needs the 24 GB of InfiniteTalk weights, a GPU or a running engine:
the asset tree is faked in a tmp dir so the contract (job.json shape, failure
text, crop normalisation, media checks) is pinned down.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import locmodel_lipsync as lipsync  # noqa: E402
import locmodel_mcp as mcp  # noqa: E402


# --------------------------------------------------------------------------
# fake LocModel install
# --------------------------------------------------------------------------

def _touch(path: Path, data: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")
    return path


@pytest.fixture()
def fake_install(tmp_path):
    """A complete-looking lipsync install: models + venv + InfiniteTalk code."""
    prog = tmp_path / "lipsync"          # program side
    mdl = tmp_path / "library" / "lipsync"  # model side
    _touch(prog / "runner.py", "# runner\n")
    py = _touch(prog / "venv" / "Scripts" / "python.exe")
    _touch(prog / "code" / "wan" / "modules" / "attention.py", "# attn\n")
    _touch(mdl / "dit" / "Wan2.1-I2V-14B-480P-Q4_K_M.gguf")
    _touch(mdl / "dit" / "InfiniteTalk-Single-Q4_K_M.gguf")
    _touch(mdl / "lora" / "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors")
    _touch(mdl / "base" / "config.json", "{}")
    _touch(mdl / "base" / "google" / "umt5-xxl" / "tokenizer.json", "{}")
    _touch(mdl / "base" / "xlm-roberta-large" / "tokenizer.json", "{}")
    _touch(mdl / "base" / "Wan2.1_VAE.pth")
    _touch(mdl / "clip" / "models_clip_open-clip-xlm-roberta-large-vit-huge-14.pth")
    _touch(mdl / "t5" / "t5_fp8.safetensors")
    _touch(mdl / "t5" / "t5_map_fp8.json", "{}")
    _touch(mdl / "wav2vec" / "preprocessor_config.json", "{}")
    _touch(mdl / "demucs" / "955717e8-8726e21a.th")
    cfg = {"root": str(prog), "models_root": str(mdl)}
    return {"cfg": cfg, "prog": prog, "models": mdl, "python": py}


def _img(tmp_path, name="face.png"):
    return _touch(tmp_path / name, "png")


def _wav(tmp_path, name="song.mp3"):
    return _touch(tmp_path / name, "mp3")


# --------------------------------------------------------------------------
# status / readiness
# --------------------------------------------------------------------------

def test_status_reports_ready_when_everything_is_mounted(fake_install):
    rep = lipsync.status(fake_install["cfg"])
    assert rep["ok"] is True, rep["missing"]
    assert rep["mount_required"] is True
    assert rep["paths"]["python"].endswith("python.exe")
    assert rep["models"]["dit"].endswith(".gguf")
    assert "对口型视频" in rep["sub_panel"]


def test_status_blocks_with_explicit_failure_message(tmp_path):
    rep = lipsync.status({"root": str(tmp_path / "lipsync"),
                          "models_root": str(tmp_path / "nope")})
    assert rep["ok"] is False
    assert rep["missing"], "must name the missing assets"
    assert "整体失败" in rep["blocking"]


def test_status_notes_missing_lora_is_not_fatal(fake_install):
    for f in (fake_install["models"] / "lora").glob("*.safetensors"):
        f.unlink()
    rep = lipsync.status(fake_install["cfg"], refresh=True)
    assert rep["ok"] is True
    assert any("LoRA" in n for n in rep["notes"])


def test_generate_refuses_to_run_without_models(tmp_path):
    cfg = {"root": str(tmp_path / "lipsync"), "models_root": str(tmp_path / "nope")}
    with pytest.raises(lipsync.LipsyncError) as err:
        lipsync.generate(str(_img(tmp_path)), str(_wav(tmp_path)), cfg)
    assert err.value.code == "lipsync_not_mounted"
    assert "整体任务失败" in str(err.value)


def test_refresh_flag_reuses_cached_scan(fake_install):
    first = lipsync.scan(fake_install["cfg"], refresh=True)
    (fake_install["models"] / "demucs" / "955717e8-8726e21a.th").unlink()
    cached = lipsync.scan(fake_install["cfg"], refresh=False)
    fresh = lipsync.scan(fake_install["cfg"], refresh=True)
    assert cached["demucs"] == first["demucs"]
    assert fresh["demucs"] is None


# --------------------------------------------------------------------------
# job.json contract — this is what LocModel's runner.py actually consumes
# --------------------------------------------------------------------------

@pytest.fixture()
def mounted(fake_install, monkeypatch):
    """build_job reads the config through module state (same path generate() takes)."""
    monkeypatch.setitem(lipsync._LOADED, "cfg", fake_install["cfg"])
    lipsync._SCAN_CACHE.clear()
    return fake_install


def test_build_job_matches_runner_contract(mounted, tmp_path):
    img, aud = _img(tmp_path), _wav(tmp_path)
    job = lipsync.build_job(str(img), str(aud), res="480",
                            out_path=tmp_path / "out.mp4", work_dir=tmp_path / "w")
    for key in ("image", "audio", "models", "work_dir", "out", "code_dir", "res"):
        assert key in job, key
    for key in ("base", "dit", "infinitetalk", "wav2vec", "demucs", "t5_dir"):
        assert job["models"].get(key), key
    assert job["res"] == "480"
    assert job["accel"] is True
    assert "width" not in job  # buckets decide the size, not us


def test_build_job_rejects_custom_without_size(mounted, tmp_path):
    with pytest.raises(lipsync.LipsyncError) as err:
        lipsync.build_job(str(_img(tmp_path)), str(_wav(tmp_path)),
                          res="custom", out_path=tmp_path / "o.mp4", work_dir=tmp_path)
    assert err.value.code == "bad_res"


def test_build_job_custom_size_is_passed_through(mounted, tmp_path):
    """Bucketing happens inside LocModel's runner; we must not silently rewrite w/h."""
    job = lipsync.build_job(str(_img(tmp_path)), str(_wav(tmp_path)),
                            res="custom", width=832, height=480,
                            out_path=tmp_path / "o.mp4", work_dir=tmp_path)
    assert (job["width"], job["height"]) == (832, 480)


def test_build_job_rejects_out_of_range_custom_size(mounted, tmp_path):
    with pytest.raises(lipsync.LipsyncError) as err:
        lipsync.build_job(str(_img(tmp_path)), str(_wav(tmp_path)),
                          res="custom", width=128, height=4096,
                          out_path=tmp_path / "o.mp4", work_dir=tmp_path)
    assert err.value.code == "bad_res"


def test_build_job_passes_steps_seed_and_audio_window(mounted, tmp_path):
    job = lipsync.build_job(str(_img(tmp_path)), str(_wav(tmp_path)),
                            steps=8, seed=42, max_seconds=12.5,
                            out_path=tmp_path / "o.mp4", work_dir=tmp_path)
    assert (job["steps"], job["seed"], job["max_seconds"]) == (8, 42, 12.5)


# --------------------------------------------------------------------------
# crop + media validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [(None, None), ("full", None), ("none", None)])
def test_crop_empty_values_mean_full_frame(value, expected):
    assert lipsync.norm_crop(value) == expected


def test_crop_head_and_upper_are_squares_and_halves():
    head = lipsync.norm_crop("head")
    assert head["w"] == head["h"]
    assert 0.0 <= head["y"] <= 0.5
    upper = lipsync.norm_crop("upper")
    assert upper["y"] == 0.0
    assert 0.5 <= upper["h"] < 0.8


def test_crop_explicit_box_is_validated():
    box = lipsync.norm_crop({"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5})
    assert box == {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}
    with pytest.raises(lipsync.LipsyncError):
        lipsync.norm_crop({"x": 0.8, "y": 0.1, "w": 0.5, "h": 0.5})
    with pytest.raises(lipsync.LipsyncError):
        lipsync.norm_crop({"x": 0.1})


def test_missing_or_wrong_media_is_rejected(tmp_path):
    with pytest.raises(lipsync.LipsyncError) as err:
        lipsync._check_media(str(tmp_path / "ghost.png"), lipsync.IMAGE_EXT, "人物图片")
    assert err.value.code == "file_missing"
    with pytest.raises(lipsync.LipsyncError) as err:
        lipsync._check_media(str(_touch(tmp_path / "a.txt")), lipsync.IMAGE_EXT, "人物图片")
    assert err.value.code == "bad_media"


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------

def test_mcp_exposes_lipsync_tools():
    names = [t["name"] for t in mcp.TOOLS]
    assert "lipsync_status" in names
    assert "lipsync_generate" in names
    gen = next(t for t in mcp.TOOLS if t["name"] == "lipsync_generate")
    assert gen["inputSchema"]["required"] == ["image", "audio"]


def test_mcp_lipsync_generate_needs_both_inputs(monkeypatch):
    res = mcp.call_tool("lipsync_generate", {"image": "C:/a.png"})
    assert res.get("isError") is True
    assert "audio" in res["content"][0]["text"]


def test_mcp_lipsync_status_does_not_need_the_engine(monkeypatch):
    """No sd-server on 8686: status must still answer instead of erroring out."""
    monkeypatch.setattr(mcp, "_require_core", lambda: "core unavailable")
    res = mcp.call_tool("lipsync_status", {"refresh": False})
    text = res["content"][0]["text"]
    assert "对口型视频" in text or "lipsync" in text


def test_mcp_lipsync_dry_run_writes_job_json(fake_install, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp.core, "load_config", lambda: fake_install["cfg"])
    res = mcp.call_tool("lipsync_generate", {
        "image": str(_img(tmp_path)), "audio": str(_wav(tmp_path)),
        "res": "custom", "width": 832, "height": 480, "dry_run": True,
        "output_dir": str(tmp_path / "out"),
    })
    text = res["content"][0]["text"]
    assert "dry run" in text
    jobs = list((fake_install["prog"] / "jobs").glob("job-*/job.json"))
    assert jobs, "job.json must be written for inspection"
    job = json.loads(jobs[0].read_text(encoding="utf-8"))
    assert job["res"] == "custom" and job["width"] == 832 and job["height"] == 480


# --------------------------------------------------------------------------
# the two real on-disk layouts (LocModel 3.6.1 "一键准备" 实测文件名)
# --------------------------------------------------------------------------

def _make_layout(tmp_path, files):
    prog = tmp_path / "lipsync"
    mdl = tmp_path / "library" / "lipsync"
    _touch(prog / "runner.py", "# runner\n")
    _touch(prog / "venv" / "Scripts" / "python.exe")
    _touch(prog / "code" / "wan" / "modules" / "attention.py", "# attn\n")
    for rel in files:
        _touch(mdl / rel)
    return {"root": str(prog), "models_root": str(mdl)}, mdl


SMALL_FILES = [
    "dit/wan2.1-i2v-14b-480p-Q4_K_M.gguf",
    "single/Wan2_1-InfiniteTalk_Single_Q4_K_M.gguf",
    "lora/lightx2v_I2V_14B_480p_rank64_bf16.safetensors",
    "split_files/vae/wan_2.1_vae.safetensors",          # 小写 + 不在 base/ 下
    "models_clip_open-clip-xlm-roberta-large-vit-huge-14.pth",
    "umt5-xxl-encoder-Q8_0.gguf",                        # GGUF 文本编码器
    "base/google/umt5-xxl/tokenizer.json",
    "base/xlm-roberta-large/tokenizer.json",
    "wav2vec/preprocessor_config.json",
    "demucs/955717e8-8726e21a.th",
]

HIGH_FILES = [
    "dit/Wan2_1-I2V-14B-480P_fp8_e4m3fn.safetensors",
    "single/infinitetalk.safetensors",
    "lora/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors",
    "base/Wan2.1_VAE.pth",
    "base/config.json",
    "base/models_clip_open-clip-xlm-roberta-large-vit-huge-14.pth",
    "quant_models/t5_fp8.safetensors",
    "quant_models/t5_map_fp8.json",
    "base/google/umt5-xxl/tokenizer.json",
    "base/xlm-roberta-large/tokenizer.json",
    "wav2vec/preprocessor_config.json",
    "demucs/955717e8-8726e21a.th",
]


def test_small_layout_gguf_text_encoder_counts_as_ready(tmp_path):
    """省内存版把 t5 换成 umt5-xxl-encoder-Q8_0.gguf，且没有 t5_map / base/config.json。

    旧扫描只认 *.safetensors，会把装好的省内存版误判成"未挂载"。
    """
    cfg, _ = _make_layout(tmp_path, SMALL_FILES)
    rep = lipsync.status(cfg)
    assert rep["ok"] is True, rep["missing"]
    assert rep["models"]["t5"].endswith("umt5-xxl-encoder-Q8_0.gguf")
    assert rep["models"]["vae"].endswith("wan_2.1_vae.safetensors")
    # base 由 tokenizer 的 base/ 祖先推出（省内存档 VAE 不在 base 下，不能拿 VAE 当锚点）
    assert rep["models"]["base"].endswith("lipsync"), rep["models"]["base"]
    assert rep["models"]["umt5"].startswith(rep["models"]["base"]), rep["models"]["umt5"]
    assert rep["models"]["dit"].endswith("wan2.1-i2v-14b-480p-Q4_K_M.gguf")


def test_high_layout_fp8_weights_are_ready(tmp_path):
    cfg, _ = _make_layout(tmp_path, HIGH_FILES)
    rep = lipsync.status(cfg)
    assert rep["ok"] is True, rep["missing"]
    assert rep["models"]["dit"].endswith("fp8_e4m3fn.safetensors")
    assert rep["models"]["infinitetalk"].endswith("single/infinitetalk.safetensors"
                                                  .split("/")[-1]) or \
        rep["models"]["infinitetalk"].endswith("infinitetalk.safetensors")
    assert rep["models"]["t5"].endswith("t5_fp8.safetensors")


def test_small_layout_does_not_warn_about_missing_fp8_map(tmp_path):
    cfg, _ = _make_layout(tmp_path, SMALL_FILES)
    rep = lipsync.status(cfg)
    assert not any("t5_map_fp8.json" in n for n in rep["notes"]), rep["notes"]


def test_variant_tables_carry_the_real_download_names():
    small = " ".join(lipsync.VARIANTS["small"]["files"])
    high = " ".join(lipsync.VARIANTS["high"]["files"])
    for token in ("wan2.1-i2v-14b-480p-Q4_K_M.gguf", "Wan2_1-InfiniteTalk_Single_Q4_K_M.gguf",
                  "umt5-xxl-encoder-Q8_0.gguf", "wan_2.1_vae.safetensors"):
        assert token in small, token
    for token in ("Wan2_1-I2V-14B-480P_fp8_e4m3fn.safetensors", "infinitetalk.safetensors",
                  "t5_fp8.safetensors", "Wan2.1_VAE.pth"):
        assert token in high, token


def test_pipeline_facts_name_the_host_commands_and_evidence():
    facts = lipsync.PIPELINE_FACTS
    joined = " ".join(facts["host_commands"])
    for cmd in ("lipsync_set_variant", "lipsync_setup", "lipsync_generate", "lipsync_cancel"):
        assert cmd in joined, cmd
    assert "LocModel.exe" in facts["evidence"]
    assert "InfiniteTalk" in facts["engine"]


def test_mcp_status_honours_the_models_dir_override(tmp_path):
    cfg, _ = _make_layout(tmp_path, SMALL_FILES)
    out = mcp.call_tool("lipsync_status", {"models_dir": cfg["models_root"]})
    # 读原始 text（json.dumps 会把反斜杠二次转义，路径就断开了）
    text = out["content"][0]["text"]
    assert f"models_root: {cfg['models_root']}" in text, text[:400]
    assert "found: " in text and "umt5-xxl-encoder-Q8_0.gguf" in text
