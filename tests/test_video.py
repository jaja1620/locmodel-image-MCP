#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tests for the engine-native video bridge (POST /sdcpp/v1/vid_gen).

The engine is faked: no GPU, no 3.4 GB Wan weights, no job that takes two
minutes. What is pinned down here is the *contract* — payload layout, the
client-side guards that stand in for the engine's silent acceptance, the
job lifecycle and the file writing (webm/avi magic bytes).
"""

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import locmodel_video as video  # noqa: E402
import locmodel_mcp as mcp  # noqa: E402


@pytest.fixture()
def workdir():
    """pytest's ``tmp_path`` cannot be used on this host: the numbered-dir
    cleanup hits WinError 5 on the managed profile. Each test gets its own
    scratch directory instead; it is left behind deliberately (removing it
    fails for the same reason)."""
    base = os.environ.get("LOCMODEL_TEST_TMP") or tempfile.gettempdir()
    return Path(tempfile.mkdtemp(prefix="locmodel-video-", dir=base))


CAPS = {
    "supported_modes": ["vid_gen"],
    "current_mode": "vid_gen",
    "limits": {"min_width": 64, "max_width": 4096, "min_height": 64, "max_height": 4096},
    "output_formats_by_mode": {"vid_gen": ["webm", "webp", "avi"]},
    "defaults_by_mode": {"vid_gen": {"fps": 16, "video_frames": 17, "width": 832, "height": 480}},
    "features": {"init_image": True, "ref_images": True, "high_noise_sample_params": False},
    "model": {"name": "Wan2.2-TI2V-5B-Q4_K_M.gguf"},
}

WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 64          # EBML magic
AVI = b"RIFF" + b"\x00\x00\x00\x00" + b"AVI " + b"\x00" * 54
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 52


@pytest.fixture()
def engine(monkeypatch):
    """Pretend an engine is listening in vid_gen mode; record every submit."""
    state = {"submit": [], "jobs": {}, "cancelled": []}

    monkeypatch.setattr(video.core, "base_url", lambda cfg=None: ("http://127.0.0.1:8790", 8790, "port"))
    monkeypatch.setattr(video.core, "load_config", lambda: {})

    def fake_caps(base, cfg):
        return dict(CAPS)

    def fake_http_json(url, payload=None, timeout=60):
        if url.endswith("/capabilities"):
            return 200, dict(CAPS)
        if url.endswith("/recipes"):
            return 200, {"recipes": [
                {"id": "wan22-ti2v-5b", "modes": ["vid_gen"], "size_multiple": 8,
                 "required_any": [["vae", "tae"]], "slots": [{"key": "vae"}, {"key": "tae"}],
                 "defaults": {"fps": 16}},
                {"id": "flux2-klein", "modes": ["img_gen"], "size_multiple": 16,
                 "required_any": [["vae"]], "slots": [], "defaults": {}},
            ]}
        if url.endswith("/cancel"):
            state["cancelled"].append(url)
            return 200, {"status": "cancel_requested"}
        if url.endswith("/vid_gen"):
            state["submit"].append(payload)
            jid = state.get("force_id") or "job_test_%04d" % len(state["submit"])
            # a test can pre-arm the outcome of the next submit
            state["jobs"][jid] = state.pop("next_job", None) or {"status": "queued"}
            return 202, {"id": jid, "kind": "vid_gen",
                         "poll_url": "/sdcpp/v1/jobs/" + jid, "status": "queued"}
        if "/jobs/" in url:
            jid = url.rsplit("/", 1)[-1]
            return 200, state["jobs"].get(jid, {"status": "completed",
                                                "result": {"b64_json": base64.b64encode(
                                                    WEBM).decode(), "fps": 16,
                                                    "frame_count": 9,
                                                    "output_format": "webm",
                                                    "mime_type": "video/webm"}})
        return 404, {"error": "not found"}

    monkeypatch.setattr(video.core, "capabilities", fake_caps)
    monkeypatch.setattr(video.core, "http_json", fake_http_json)
    state["caps"] = fake_caps
    return state


# --------------------------------------------------------------------------
# payload
# --------------------------------------------------------------------------

def test_payload_matches_engine_layout(engine):
    payload = video.build_payload("a cat walking", "blurry", width=640, height=384,
                                  fps=16, video_frames=9, steps=2, flow_shift=12.0,
                                  cfg_scale=5.0, seed=42, info=video.engine_mode({}))
    assert payload["prompt"] == "a cat walking"
    assert payload["negative_prompt"] == "blurry"
    assert (payload["width"], payload["height"]) == (640, 384)
    assert payload["sample_params"] == {"sample_steps": 2, "flow_shift": 12.0,
                                        "guidance": {"txt_cfg": 5.0}}
    assert payload["output_format"] == "webm"
    assert payload["seed"] == 42


def test_scheduler_default_is_rejected_before_the_engine(engine):
    with pytest.raises(video.VideoError) as exc:
        video.build_payload("x", scheduler="default", info=video.engine_mode({}))
    assert "scheduler" in str(exc.value)


def test_unknown_sample_key_is_rejected(engine):
    with pytest.raises(video.VideoError) as exc:
        video._clean_sample_params({"slg_scale": 1.0})
    assert "slg_scale" in str(exc.value)


def test_high_noise_keys_are_opt_in(engine):
    info = video.engine_mode({})
    assert "high_noise_sample_params" not in video.build_payload("x", info=info)
    p = video.build_payload("x", high_noise_steps=10, info=info)
    assert p["high_noise_sample_params"] == {"sample_steps": 10}


def test_size_guards(engine):
    info = video.engine_mode({})
    with pytest.raises(video.VideoError):
        video.build_payload("x", width=100, height=64, info=info)   # not multiple of 8
    with pytest.raises(video.VideoError):
        video.build_payload("x", width=32, height=32, info=info)    # below min
    with pytest.raises(video.VideoError):
        video.build_payload("x", output_format="png", info=info)


def test_empty_prompt_rejected(engine):
    with pytest.raises(video.VideoError):
        video.build_payload("   ", info=video.engine_mode({}))


def test_image_paths_become_data_uris(engine, workdir):
    img = workdir / "face.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    info = video.engine_mode({})
    p = video.build_payload("x", init_image=str(img), ref_images=[str(img)], info=info)
    assert p["init_image"].startswith("data:image/png;base64,")
    assert p["ref_images"][0].startswith("data:image/png;base64,")


def test_missing_image_raises(engine, workdir):
    with pytest.raises(video.VideoError):
        video.build_payload("x", init_image=str(workdir / "nope.png"),
                            info=video.engine_mode({}))


# --------------------------------------------------------------------------
# mode guard
# --------------------------------------------------------------------------

def test_non_video_engine_is_reported_clearly(engine, monkeypatch):
    img_caps = dict(CAPS, supported_modes=["img_gen"], current_mode="img_gen")
    monkeypatch.setattr(video.core, "capabilities", lambda base, cfg: dict(img_caps))
    with pytest.raises(video.VideoError) as exc:
        video.generate("x")
    assert exc.value.code == "engine_not_in_video_mode"
    assert "视频" in exc.value.hint


# --------------------------------------------------------------------------
# job lifecycle
# --------------------------------------------------------------------------

def test_generate_returns_job_id_without_waiting(engine):
    res = video.generate("a cat", width=832, height=480)
    assert res["ok"] and res["job_id"].startswith("job_test_")
    assert res["files"] == []
    assert len(engine["submit"]) == 1


def test_generate_wait_saves_webm(engine, workdir):
    engine["next_job"] = {
        "status": "completed",
        "result": {"b64_json": base64.b64encode(WEBM).decode(), "fps": 16,
                   "frame_count": 9, "output_format": "webm", "mime_type": "video/webm"},
        "progress": {"stage": "encoding"},
    }
    res = video.generate("a cat", wait_seconds=10, output_dir=str(workdir))
    assert res["ok"], res
    files = res["files"]
    assert len(files) == 1 and files[0]["path"].endswith(".webm")
    assert Path(files[0]["path"]).read_bytes()[:4] == WEBM[:4]


def test_wrong_extension_is_corrected_from_magic_bytes(engine, workdir):
    engine["next_job"] = {
        "status": "completed",
        "result": {"b64_json": base64.b64encode(AVI).decode(), "output_format": "webm"},
    }
    files = video.generate("a cat", wait_seconds=5, output_dir=str(workdir))["files"]
    assert files[0]["path"].endswith(".avi")


def test_status_saves_completed_job(engine, workdir):
    engine["jobs"]["job_x"] = {"status": "completed",
                               "result": {"b64_json": base64.b64encode(WEBP).decode(),
                                          "output_format": "webp"}}
    res = video.status("job_x", output_dir=str(workdir))
    assert res["status"] == "completed"
    assert res["files"][0]["path"].endswith(".webp")


def test_status_running_job_has_no_files(engine):
    engine["jobs"]["job_r"] = {"status": "running", "progress": {"stage": "denoising"}}
    res = video.status("job_r", save_result=True)
    assert res["files"] == [] and res["progress"]["stage"] == "denoising"


def test_cancel(engine):
    assert video.cancel("job_c")["status"] == "cancel_requested"
    assert engine["cancelled"]


def test_wait_times_out(engine, monkeypatch):
    engine["jobs"]["job_slow"] = {"status": "running"}
    monkeypatch.setattr(video.time, "sleep", lambda s: None)
    job = video.wait("job_slow", timeout=0.01)
    assert job.get("timed_out") is True


# --------------------------------------------------------------------------
# recipes
# --------------------------------------------------------------------------

def test_recipes_video_filter(engine):
    res = video.recipes("video")
    ids = [r["id"] for r in res["recipes"]]
    assert "wan22-ti2v-5b" in ids and "flux2-klein" not in ids


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------

def test_tool_names_registered():
    names = {t["name"] for t in mcp.TOOLS}
    assert {"generate_video", "job_status", "cancel_job", "list_recipes"} <= names
    allnames = [t["name"] for t in mcp.TOOLS]
    assert len(allnames) == len(set(allnames)), "duplicate tool name"


def test_mcp_generate_video_returns_poll_hint(engine):
    out = mcp.call_tool("generate_video", {"prompt": "a cat", "width": 832, "height": 480})
    text = out["content"][0]["text"]
    assert "job_status" in text
    assert "job_test_" in text


def test_mcp_dry_run_does_not_submit(engine):
    out = mcp.call_tool("generate_video", {"prompt": "a cat", "dry_run": True})
    assert "dry_run" in out["content"][0]["text"]
    assert engine["submit"] == []


def test_mcp_job_status(engine, workdir):
    engine["jobs"]["job_m"] = {"status": "failed", "error": {"message": "generate_video returned no results"}}
    out = mcp.call_tool("job_status", {"job_id": "job_m"})
    text = out["content"][0]["text"]
    assert "failed" in text and "no results" in text


def test_mcp_missing_job_id(engine):
    out = mcp.call_tool("job_status", {})
    assert "job_id" in out["content"][0]["text"]


def test_mcp_generate_video_rejects_empty_prompt(engine):
    out = mcp.call_tool("generate_video", {"prompt": ""})
    assert "prompt" in out["content"][0]["text"]
