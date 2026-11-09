#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests that need neither a GPU nor a running engine.

Run:  python -m pytest tests -q
"""

import base64
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import locmodel_image as core  # noqa: E402
import locmodel_mcp as mcp  # noqa: E402


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def test_deep_merge_keeps_untouched_keys():
    merged = core._deep_merge({"a": {"x": 1, "y": 2}, "b": 3}, {"a": {"y": 9}})
    assert merged == {"a": {"x": 1, "y": 9}, "b": 3}


def test_cfg_get_dotted_and_fallback():
    cfg = {"size": {"min_area": 10}, "port": None}
    assert core.cfg_get(cfg, "size.min_area") == 10
    assert core.cfg_get(cfg, "size.nope", "fb") == "fb"
    assert core.cfg_get(cfg, "missing.deep.key", 7) == 7


def test_load_config_applies_env_port(monkeypatch):
    monkeypatch.setenv("LOCMODEL_SD_PORT", "8899")
    cfg = core.load_config()
    assert cfg["port"] == 8899


def test_save_and_reload_config(tmp_path):
    path = tmp_path / "cfg.json"
    cfg = core.load_config(tmp_path / "none.json")
    cfg["size"]["min_area"] = 123456
    core.save_config(cfg, path)
    reloaded = core.load_config(path)
    assert reloaded["size"]["min_area"] == 123456
    assert reloaded["size"]["multiple"] == core.DEFAULTS["size"]["multiple"]


def test_coerce_types():
    assert core._coerce("123") == 123
    assert core._coerce("1.5") == 1.5
    assert core._coerce("true") is True
    assert core._coerce("hello") == "hello"


# --------------------------------------------------------------------------
# size policy
# --------------------------------------------------------------------------

@pytest.fixture()
def cfg():
    return json.loads(json.dumps(core.DEFAULTS))


@pytest.mark.parametrize("w,h,expected", [
    (1280, 720, (1280, 720)),   # above min area, keeps aspect ratio
    (720, 1280, (720, 1280)),   # portrait preserved
    (1024, 1024, (1024, 1024)),
])
def test_normalize_size_preserves_valid_requests(cfg, w, h, expected):
    got_w, got_h, notes = core.normalize_size(w, h, cfg, guard=True)
    assert (got_w, got_h) == expected
    assert notes == []


def test_normalize_size_upscales_by_area_not_by_edge(cfg):
    w, h, notes = core.normalize_size(640, 640, cfg)
    assert w * h >= core.DEFAULTS["size"]["min_area"]
    assert notes and "upscaled" in notes[0]


def test_normalize_size_reduces_huge_requests(cfg):
    w, h, notes = core.normalize_size(3000, 2000, cfg, guard=True)
    assert max(w, h) <= core._size_policy(cfg, None)["max_side"]
    assert notes and "reduced" in notes[0]


def test_normalize_size_aligns_to_multiple(cfg):
    w, h, _ = core.normalize_size(1001, 777, cfg, guard=False)
    assert w % 8 == 0 and h % 8 == 0


def test_normalize_size_guard_can_be_disabled(cfg):
    w, h, notes = core.normalize_size(512, 512, cfg, guard=False)
    assert (w, h) == (512, 512) and notes == []


def test_normalize_size_rejects_bad_values(cfg):
    with pytest.raises(core.BridgeError):
        core.normalize_size(0, 512, cfg)
    with pytest.raises(core.BridgeError):
        core.normalize_size("x", 512, cfg)


def test_size_policy_follows_vram(cfg):
    low = core._size_policy(cfg, {"total_mb": 4096})
    high = core._size_policy(cfg, {"total_mb": 16384})
    assert low["max_side"] < high["max_side"] <= core.DEFAULTS["size"]["hard_max_side"]


@pytest.mark.parametrize("aspect", ["1:1", "16:9", "9:16", "4:3", "2:1"])
def test_aspect_to_size_roundtrip(aspect):
    w, h = core.aspect_to_size(aspect, 1024 * 1024)
    a, b = core.ASPECTS[aspect]
    assert abs(w / h - a / b) < 0.08


def test_aspect_unknown_raises():
    with pytest.raises(core.BridgeError):
        core.aspect_to_size("7:5")


# --------------------------------------------------------------------------
# failure taxonomy / helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize("msg,kind", [
    ("generate_image returned no results", "capacity"),
    ("failed during workspace capacity check", "capacity"),
    ("out of memory", "capacity"),
    ("model not loaded", "parameter"),
    ("unsupported size", "parameter"),
    ("network hiccup", "transient"),
])
def test_classify_failure(msg, kind):
    assert core._classify_failure(msg) == kind


def test_is_loopback():
    assert core._is_loopback("http://127.0.0.1:8790/a.png")
    assert core._is_loopback("http://localhost:8790/a.png")
    assert not core._is_loopback("http://example.com/a.png")


def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_decode_image_from_b64():
    raw = _png_bytes()
    img = {"b64_json": base64.b64encode(raw).decode()}
    assert core._decode_image(img, 10 ** 6) == raw


def test_decode_image_data_uri():
    raw = _png_bytes()
    img = {"b64_json": "data:image/png;base64," + base64.b64encode(raw).decode()}
    assert core._decode_image(img, 10 ** 6) == raw


def test_decode_image_rejects_remote_url():
    assert core._decode_image({"url": "http://example.com/x.png"}, 10 ** 6) is None


def test_decode_image_missing_payload():
    assert core._decode_image({}, 10 ** 6) is None


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def test_discovery_prefers_env_port(cfg, monkeypatch):
    monkeypatch.setenv("LOCMODEL_SD_PORT", "9123")
    monkeypatch.setattr(core, "probe", lambda base, cfg: base.endswith(":9123"))
    port, src = core.discover(cfg)
    assert (port, src) == (9123, "env")


def test_discovery_uses_process_scan(cfg, monkeypatch):
    monkeypatch.delenv("LOCMODEL_SD_PORT", raising=False)
    cfg["port"] = None
    monkeypatch.setattr(core, "probe", lambda base, cfg: base.endswith(":9911"))
    monkeypatch.setattr(core, "port_from_process", lambda cfg: [9911])
    port, src = core.discover(cfg)
    assert (port, src) == (9911, "process")


def test_discovery_reports_not_found(cfg, monkeypatch):
    monkeypatch.delenv("LOCMODEL_SD_PORT", raising=False)
    cfg["port"] = None
    monkeypatch.setattr(core, "probe", lambda base, cfg: False)
    monkeypatch.setattr(core, "port_from_process", lambda cfg: [])
    monkeypatch.setattr(core, "_local_listen_ports", lambda: [])
    cfg["port_candidates"] = [1, 2]
    port, src = core.discover(cfg)
    assert port is None and src == "not_found"


def test_port_from_process_parses_flags(cfg, monkeypatch):
    fake = "sd-server.exe --listen-ip 127.0.0.1 --listen-port 8790 --model x.gguf"
    monkeypatch.setattr(core, "_run", lambda cmd, timeout=20: fake)
    assert 8790 in core.port_from_process(cfg)


def test_base_url_raises_engine_not_found(cfg, monkeypatch):
    monkeypatch.setattr(core, "probe", lambda base, cfg: False)
    monkeypatch.setattr(core, "port_from_process", lambda cfg: [])
    monkeypatch.setattr(core, "_local_listen_ports", lambda: [])
    monkeypatch.delenv("LOCMODEL_SD_PORT", raising=False)
    cfg["port"] = None
    cfg["port_candidates"] = []
    with pytest.raises(core.EngineNotFound):
        core.base_url(cfg=cfg)


# --------------------------------------------------------------------------
# argument validation (must not touch the engine)
# --------------------------------------------------------------------------

def test_generate_rejects_empty_prompt(cfg):
    with pytest.raises(core.BridgeError) as exc:
        core.generate("   ", cfg)
    assert exc.value.code == "bad_arguments"


def test_generate_rejects_excessive_count(cfg):
    with pytest.raises(core.BridgeError) as exc:
        core.generate("x", cfg, count=99)
    assert "max_count" in str(exc.value)


# --------------------------------------------------------------------------
# MCP protocol
# --------------------------------------------------------------------------

def test_mcp_initialize_negotiates_known_version():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18"}})
    assert resp["result"]["protocolVersion"] == "2025-06-18"
    assert resp["result"]["serverInfo"]["name"] == "locmodel-image"


def test_mcp_initialize_falls_back_for_unknown_version():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "1999-01-01"}})
    assert resp["result"]["protocolVersion"] in mcp.SUPPORTED_PROTOCOL_VERSIONS


def test_mcp_notification_has_no_response():
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_mcp_tools_list_contract():
    tools = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"generate_image", "locmodel_status", "list_models",
            "engine_doctor", "engine_discover"} <= names
    gen = next(t for t in tools if t["name"] == "generate_image")
    assert gen["inputSchema"]["required"] == ["prompt"]
    assert "enum" in gen["inputSchema"]["properties"]["aspect"]


def test_mcp_unknown_method():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "nope/nope"})
    assert resp["error"]["code"] == -32601


def test_mcp_ping():
    assert mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "ping"})["result"] == {}


def test_mcp_generate_requires_prompt():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "generate_image", "arguments": {}}})
    assert resp["result"]["isError"] is True


def test_mcp_unknown_tool_is_error():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                       "params": {"name": "does_not_exist", "arguments": {}}})
    assert resp["result"]["isError"] is True
