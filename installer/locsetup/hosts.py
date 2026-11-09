"""Host (agent client) adapters.

Each host is described declaratively: where its config lives, which section key
holds the server map, and what extra fields its schema demands. Detection refuses
to create files for software the user does not have, so a run never litters
half-installed configs across the machine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import MCP_SERVER_KEY
from . import jsoncfg, yamlmini
from .fsutil import InstallError


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (_home() / "AppData" / "Roaming"))


@dataclass
class Host:
    id: str
    label: str
    kind: str                      # json | yaml | manual
    paths: list[Path]
    key_candidates: list[list[str]] = field(default_factory=list)
    section_candidates: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    probe_dirs: list[Path] = field(default_factory=list)
    doc: str = ""
    notes: str = ""

    # -- detection ---------------------------------------------------------
    def resolved_path(self) -> tuple[Path, str]:
        """Return (path, status) with status in {present, creatable, absent}.

        A config path is only considered "creatable" when there is real evidence
        the host is installed: either a dedicated probe directory exists, or the
        config's own parent directory exists. The user's home directory is *not*
        evidence -- almost every host keeps a dotfile there, so treating it as
        evidence would make every host look installed.
        """
        if not self.paths:  # manual hosts have no config file
            return Path(""), "absent"
        for p in self.paths:
            if p.exists():
                return p, "present"
        for d in self.probe_dirs:
            if d.exists():
                return self.paths[0], "creatable"
        try:
            home = Path(os.path.expanduser("~")).resolve()
        except Exception:
            home = None
        for p in self.paths:
            try:
                parent = p.parent
                if parent.exists() and (home is None or parent.resolve() != home):
                    return p, "creatable"
            except OSError:
                continue
        return self.paths[0], "absent"

    def is_installed_software(self) -> bool:
        _, status = self.resolved_path()
        return status in ("present", "creatable")

    def spec(self, command: str, args: list[str], env: dict | None = None) -> dict:
        spec: dict = {"command": command, "args": list(args)}
        if env:
            spec["env"] = dict(env)
        spec.update(self.extra)
        return spec

    # -- mutation ----------------------------------------------------------
    def apply(self, command: str, args: list[str], env: dict | None = None,
              *, backup: bool = True) -> dict:
        path, status = self.resolved_path()
        if status == "absent":
            return {"host": self.id, "status": "skipped", "path": str(path),
                    "reason": "software not found on this machine"}
        spec = self.spec(command, args, env)
        if self.kind == "json":
            res = jsoncfg.apply_server(path, self.key_candidates, MCP_SERVER_KEY, spec,
                                       backup=backup)
        elif self.kind == "yaml":
            res = yamlmini.apply_server(path, self.section_candidates, MCP_SERVER_KEY, spec,
                                        backup=backup)
        else:
            return {"host": self.id, "status": "manual", "path": str(path),
                    "reason": "host has no file-based config"}
        res["host"] = self.id
        res["status"] = res.get("action", "unknown")
        return res

    def strip(self, *, backup: bool = True) -> dict:
        path, status = self.resolved_path()
        if status == "absent" or not path.exists():
            return {"host": self.id, "status": "absent", "path": str(path)}
        if self.kind == "json":
            res = jsoncfg.strip_server(path, self.key_candidates, MCP_SERVER_KEY, backup=backup)
        elif self.kind == "yaml":
            res = yamlmini.strip_server(path, self.section_candidates, MCP_SERVER_KEY,
                                        backup=backup)
        else:
            return {"host": self.id, "status": "manual", "path": str(path)}
        res["host"] = self.id
        res["status"] = res.get("action", "unknown")
        return res

    def current_entry(self) -> dict | None:
        path, status = self.resolved_path()
        if status == "absent" or not path.exists() or self.kind != "json":
            return None
        try:
            data = jsoncfg.load_json_config(path)
        except InstallError:
            return None
        return jsoncfg.get_server(data, self.key_candidates[0], MCP_SERVER_KEY)


def build_hosts() -> list[Host]:
    home, appdata = _home(), _appdata()
    return [
        Host(
            id="claude-desktop",
            label="Claude Desktop",
            kind="json",
            paths=[appdata / "Claude" / "claude_desktop_config.json"],
            key_candidates=[["mcpServers"]],
            doc="https://modelcontextprotocol.io/quickstart/user",
            notes="restart Claude Desktop to pick up the new server",
        ),
        Host(
            id="claude-code",
            label="Claude Code (CLI)",
            kind="json",
            paths=[home / ".claude.json", home / ".claude" / "settings.json"],
            key_candidates=[["mcpServers"]],
            probe_dirs=[home / ".claude"],
            doc="https://docs.anthropic.com/en/docs/claude-code/mcp",
            notes="run `claude mcp list` to verify",
        ),
        Host(
            id="cursor",
            label="Cursor",
            kind="json",
            paths=[appdata / "Cursor" / "User" / "mcp.json",
                   home / ".cursor" / "mcp.json"],
            key_candidates=[["mcpServers"]],
            probe_dirs=[appdata / "Cursor"],
            doc="https://docs.cursor.com/context/model-context-protocol",
            notes="global user config; project-level .cursor/mcp.json is untouched",
        ),
        Host(
            id="vscode",
            label="VS Code / Copilot",
            kind="json",
            paths=[appdata / "Code" / "User" / "mcp.json"],
            key_candidates=[["servers"], ["mcpServers"]],
            extra={"type": "stdio"},
            probe_dirs=[appdata / "Code"],
            doc="https://code.visualstudio.com/docs/copilot/customization/mcp-servers",
            notes="VS Code uses the 'servers' key and a 'type' field",
        ),
        Host(
            id="windsurf",
            label="Windsurf",
            kind="json",
            paths=[home / ".codeium" / "windsurf" / "mcp_config.json"],
            key_candidates=[["mcpServers"]],
            probe_dirs=[home / ".codeium"],
            doc="https://docs.windsurf.com/windsurf/cascade/mcp",
        ),
        Host(
            id="dsh",
            label="DeepSeek Harness Desktop",
            kind="json",
            paths=[home / ".dsh" / "mcp.json"],
            key_candidates=[["mcpServers"]],
            probe_dirs=[home / ".dsh"],
            doc="https://github.com/deepseek-ai",
            notes="master switch flips on with the first server; agent restarts on save",
        ),
        Host(
            id="hermes",
            label="Hermes Desktop",
            kind="yaml",
            paths=[home / ".hermes" / "config.yaml", home / ".hermes" / "config.yml"],
            section_candidates=["mcp_servers", "mcpServers", "mcp"],
            probe_dirs=[home / ".hermes"],
            doc="https://github.com/NousResearch/hermes",
            notes="built-in native-mcp skill (v0.20.0+); verify with `hermes mcp list`",
        ),
        Host(
            id="marvis",
            label="Marvis",
            kind="manual",
            paths=[],
            notes="registered through the marvis_capabilities tool, not a config file",
        ),
    ]


def find_host(hosts: list[Host], ident: str) -> Host | None:
    for h in hosts:
        if h.id == ident:
            return h
    return None


def host_ids() -> list[str]:
    return [h.id for h in build_hosts()]


MANUAL_HOSTS = {"marvis"}
