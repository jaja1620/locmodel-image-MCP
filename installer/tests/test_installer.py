"""Installer test-suite (stdlib unittest, no third-party deps).

Run from the ``installer`` directory:

    python -m unittest discover -s tests -v

Two layers:

* in-process unit tests that exercise the config editors and staging against
  throw-away directories;
* isolated end-to-end tests that launch the real CLI in a subprocess with
  USERPROFILE / APPDATA redirected into a temporary sandbox, so the operator's
  real host configs are never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

INSTALLER_DIR = Path(__file__).resolve().parents[1]
if str(INSTALLER_DIR) not in sys.path:
    sys.path.insert(0, str(INSTALLER_DIR))

from locsetup import core, hosts, jsoncfg, payload, yamlmini  # noqa: E402
from locsetup.fsutil import InstallError  # noqa: E402

ENTRY = INSTALLER_DIR / "locmodel_image_setup.py"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sandbox_env(root: Path) -> dict:
    """Env that makes Path.home()/APPDATA/LOCALAPPDATA land inside *root*."""
    env = dict(os.environ)
    home = root / "home"
    roam = home / "AppData" / "Roaming"
    local = home / "AppData" / "Local"
    for d in (home, roam, local):
        d.mkdir(parents=True, exist_ok=True)
    env.update({
        "USERPROFILE": str(home),
        "HOMEDRIVE": home.drive or "C:",
        "HOMEPATH": str(home)[len(home.drive):],
        "HOME": str(home),
        "APPDATA": str(roam),
        "LOCALAPPDATA": str(local),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def run_cli(args: list[str], env: dict, *, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ENTRY), *args],
        cwd=str(INSTALLER_DIR),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# unit: JSON config editor
# ---------------------------------------------------------------------------

class TestJsonCfg(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "mcp.json"
        self.spec = {"command": "C:\\x\\setup.exe", "args": ["--serve"]}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_file_and_key_when_missing(self) -> None:
        res = jsoncfg.apply_server(self.path, [["mcpServers"]], "locmodel-image", self.spec)
        self.assertIn(res["action"], ("created", "updated"))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["locmodel-image"]["command"], self.spec["command"])

    def test_preserves_unrelated_keys_and_servers(self) -> None:
        self.path.write_text(json.dumps({
            "mcpServers": {"other": {"command": "node", "args": ["a.js"]}},
            "theme": "dark",
        }), encoding="utf-8")
        jsoncfg.apply_server(self.path, [["mcpServers"]], "locmodel-image", self.spec)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "dark")
        self.assertIn("other", data["mcpServers"])
        self.assertIn("locmodel-image", data["mcpServers"])

    def test_is_idempotent(self) -> None:
        jsoncfg.apply_server(self.path, [["mcpServers"]], "locmodel-image", self.spec)
        jsoncfg.apply_server(self.path, [["mcpServers"]], "locmodel-image", self.spec)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(data["mcpServers"]), ["locmodel-image"])

    def test_backup_is_written_before_first_modification(self) -> None:
        self.path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        jsoncfg.apply_server(self.path, [["mcpServers"]], "locmodel-image", self.spec, backup=True)
        backups = list(self.dir.glob("mcp.json.bak*"))
        self.assertTrue(backups, "expected a .bak file")

    def test_refuses_to_touch_broken_json(self) -> None:
        self.path.write_text("{ this is not json", encoding="utf-8")
        with self.assertRaises(Exception):
            jsoncfg.apply_server(self.path, [["mcpServers"]], "locmodel-image", self.spec)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{ this is not json")

    def test_detects_alternate_section_name(self) -> None:
        self.path.write_text(json.dumps({"mcp": {"servers": {}}}), encoding="utf-8")
        res = jsoncfg.apply_server(
            self.path, [["mcpServers"], ["mcp", "servers"], ["servers"]],
            "locmodel-image", self.spec)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("locmodel-image", data["mcp"]["servers"])
        self.assertEqual(res["key_path"], "mcp.servers")

    def test_strip_removes_only_our_entry(self) -> None:
        self.path.write_text(json.dumps({
            "mcpServers": {"other": {"command": "node"}, "locmodel-image": self.spec}
        }), encoding="utf-8")
        jsoncfg.strip_server(self.path, [["mcpServers"]], "locmodel-image")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(data["mcpServers"]), ["other"])


# ---------------------------------------------------------------------------
# unit: YAML (line-level) editor
# ---------------------------------------------------------------------------

class TestYamlMini(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.yaml"
        self.spec = {"command": "C:\\x\\setup.exe", "args": ["--serve"]}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_section_when_absent(self) -> None:
        self.path.write_text("model: z-image\n", encoding="utf-8")
        yamlmini.apply_server(self.path, ["mcp_servers", "mcpServers"], "locmodel-image", self.spec)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("mcp_servers:", text)
        self.assertIn("locmodel-image:", text)
        self.assertIn("model: z-image", text)

    def test_appends_to_existing_section(self) -> None:
        self.path.write_text("mcp_servers:\n  other:\n    command: node\n", encoding="utf-8")
        yamlmini.apply_server(self.path, ["mcp_servers"], "locmodel-image", self.spec)
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(text.count("mcp_servers:"), 1)
        self.assertIn("other:", text)
        self.assertIn("locmodel-image:", text)

    def test_strip_leaves_section_usable(self) -> None:
        self.path.write_text("mcp_servers:\n  locmodel-image:\n    command: x\n", encoding="utf-8")
        yamlmini.strip_server(self.path, ["mcp_servers"], "locmodel-image")
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("locmodel-image", text)


# ---------------------------------------------------------------------------
# unit: host detection semantics
# ---------------------------------------------------------------------------

class TestHostDetection(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_present_when_file_exists(self) -> None:
        f = self.root / "cfg.json"
        f.write_text("{}", encoding="utf-8")
        h = hosts.Host("t", "T", "json", [f])
        self.assertEqual(h.resolved_path()[1], "present")

    def test_creatable_when_probe_dir_exists(self) -> None:
        probe = self.root / "appdir"
        probe.mkdir()
        h = hosts.Host("t", "T", "json", [self.root / "nodir" / "cfg.json"],
                       probe_dirs=[probe])
        self.assertEqual(h.resolved_path()[1], "creatable")

    def test_absent_when_only_parent_is_home(self) -> None:
        home = Path(os.path.expanduser("~"))
        h = hosts.Host("t", "T", "json", [home / ".definitely-not-installed-xyz.json"])
        self.assertEqual(h.resolved_path()[1], "absent")

    def test_manual_host_has_no_path(self) -> None:
        h = hosts.Host("m", "M", "manual", [])
        self.assertEqual(h.resolved_path()[1], "absent")
        self.assertFalse(h.is_installed_software())


# ---------------------------------------------------------------------------
# unit: payload staging
# ---------------------------------------------------------------------------

class TestPayload(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "install"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_stage_and_verify(self) -> None:
        staged = payload.stage(self.dir, copy_exe=True)
        self.assertTrue((self.dir / "scripts" / "locmodel_mcp.py").is_file())
        self.assertTrue((self.dir / "scripts" / "locmodel_image.py").is_file())
        checks = payload.verify(self.dir)
        self.assertTrue(checks)
        self.assertTrue(all(c["ok"] for c in checks), [c for c in checks if not c["ok"]])
        self.assertTrue(staged["files"])

    def test_manifest_written(self) -> None:
        payload.stage(self.dir, copy_exe=True)
        man = payload.read_manifest(self.dir)
        self.assertIsNotNone(man)
        self.assertIn("files", man)

    def test_restage_is_idempotent(self) -> None:
        payload.stage(self.dir, copy_exe=True)
        payload.stage(self.dir, copy_exe=True)
        self.assertTrue(payload.verify(self.dir)[0]["ok"])


# ---------------------------------------------------------------------------
# unit: launch spec
# ---------------------------------------------------------------------------

class TestLaunchSpec(unittest.TestCase):
    def test_exe_mode_requires_frozen_build(self) -> None:
        opts = core.Options(mode="exe")
        with self.assertRaises(InstallError):
            core.build_launch(opts, Path("C:/tmp/i"), {"exe": None})

    def test_exe_mode_uses_serve_flag(self) -> None:
        opts = core.Options(mode="exe")
        launch = core.build_launch(opts, Path("C:/tmp/i"), {"exe": "C:/a/setup.exe"})
        self.assertEqual(launch["args"], ["--serve"])
        self.assertEqual(launch["mode"], "exe")

    def test_python_mode_points_at_staged_script(self) -> None:
        py = sys.executable
        opts = core.Options(mode="python", python=py)
        launch = core.build_launch(opts, Path("C:/tmp/i"), {})
        self.assertEqual(launch["command"], py)
        self.assertTrue(launch["args"][0].endswith("locmodel_mcp.py"))

    def test_dry_run_tolerates_missing_interpreter(self) -> None:
        opts = core.Options(mode="python", python=None, dry_run=True)
        launch = core.build_launch(opts, Path("C:/tmp/i"), {}, allow_pending=True)
        self.assertIn("mode", launch)


# ---------------------------------------------------------------------------
# isolated end-to-end: the real CLI in a fake user profile
# ---------------------------------------------------------------------------

class TestEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env = sandbox_env(self.root)
        self.home = Path(self.env["USERPROFILE"])
        self.roaming = Path(self.env["APPDATA"])
        self.install_dir = self.root / "prog"
        # pretend both hosts are installed
        (self.home / ".dsh").mkdir(parents=True, exist_ok=True)
        (self.roaming / "Claude").mkdir(parents=True, exist_ok=True)
        self.claude_cfg = self.roaming / "Claude" / "claude_desktop_config.json"
        self.dsh_cfg = self.home / ".dsh" / "mcp.json"
        self.base = ["--dir", str(self.install_dir),
                     "--config", str(self.home / ".locmodel-image" / "config.json"),
                     "--python", sys.executable]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _install(self, extra: list[str] | None = None) -> subprocess.CompletedProcess:
        return run_cli(["install", *self.base, "-y", "--skip-engine", *(extra or [])], self.env)

    def test_full_install_then_uninstall(self) -> None:
        r = self._install(["--targets", "claude-desktop,dsh"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        # runtime files landed
        self.assertTrue((self.install_dir / "scripts" / "locmodel_mcp.py").is_file())
        self.assertTrue((self.install_dir / "SKILL.md").is_file())
        self.assertTrue((self.home / ".locmodel-image" / "config.json").is_file())

        # both host configs carry our entry, pointing at the staged script
        for cfg in (self.claude_cfg, self.dsh_cfg):
            self.assertTrue(cfg.is_file(), cfg)
            data = json.loads(cfg.read_text(encoding="utf-8"))
            entry = data["mcpServers"]["locmodel-image"]
            self.assertEqual(entry["command"], sys.executable)
            self.assertTrue(entry["args"][0].endswith("locmodel_mcp.py"))
            self.assertTrue(str(Path(entry["args"][0]).parent).endswith("scripts"))

        # install report
        report_path = self.install_dir / "last-install-report.json"
        self.assertTrue(report_path.is_file())
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(report["ok"], report.get("checks"))
        self.assertTrue(report["smoke_ok"])

        # second run must be idempotent
        r2 = self._install(["--targets", "claude-desktop,dsh"])
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        data = json.loads(self.claude_cfg.read_text(encoding="utf-8"))
        self.assertEqual(list(data["mcpServers"]), ["locmodel-image"])

        # uninstall removes the entry, keeps the rest
        r3 = run_cli(["uninstall", *self.base, "-y"], self.env)
        self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
        data = json.loads(self.claude_cfg.read_text(encoding="utf-8"))
        self.assertNotIn("locmodel-image", data.get("mcpServers", {}))

    def test_install_preserves_existing_servers(self) -> None:
        self.claude_cfg.write_text(json.dumps({
            "mcpServers": {"keepme": {"command": "node", "args": ["keep.js"]}},
            "globalShortcut": "Ctrl+Space",
        }), encoding="utf-8")
        r = self._install(["--targets", "claude-desktop"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = json.loads(self.claude_cfg.read_text(encoding="utf-8"))
        self.assertIn("keepme", data["mcpServers"])
        self.assertIn("locmodel-image", data["mcpServers"])
        self.assertEqual(data["globalShortcut"], "Ctrl+Space")

    def test_dry_run_writes_nothing(self) -> None:
        r = run_cli(["install", *self.base, "--dry-run", "--targets", "claude-desktop,dsh"], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.claude_cfg.exists())
        self.assertFalse(self.dsh_cfg.exists())
        self.assertFalse(self.install_dir.exists())

    def test_doctor_after_install(self) -> None:
        self.assertEqual(self._install(["--targets", "dsh"]).returncode, 0)
        r = run_cli(["doctor", *self.base, "--skip-engine", "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        payload_json = json.loads(r.stdout)
        self.assertTrue(payload_json["ok"])

    def test_list_reports_registration(self) -> None:
        self.assertEqual(self._install(["--targets", "dsh"]).returncode, 0)
        r = run_cli(["list", *self.base, "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        state = json.loads(r.stdout)
        dsh = next(h for h in state["hosts"] if h["host"] == "dsh")
        self.assertTrue(dsh["registered"])

    def test_emit_marvis_mentions_both_tools(self) -> None:
        r = run_cli(["emit", "marvis", *self.base], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("mcp_install", r.stdout)
        self.assertIn("skill_install", r.stdout)


# ---------------------------------------------------------------------------
# MCP stdio handshake against the served entry point
# ---------------------------------------------------------------------------

class TestServeHandshake(unittest.TestCase):
    def test_initialize_and_tools_list(self) -> None:
        from locsetup import selftest
        res = selftest.mcp_smoke([sys.executable, str(ENTRY), "serve"],
                                 cwd=INSTALLER_DIR, timeout=90.0, call_doctor=False)
        self.assertTrue(res["ok"], res)
        names = {c["name"] for c in res["checks"]}
        self.assertIn("mcp.initialize", names)
        self.assertIn("mcp.tools_list", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
