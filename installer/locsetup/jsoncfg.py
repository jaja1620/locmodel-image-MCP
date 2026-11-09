"""Minimal-JSON config editing.

Rules enforced here (these are the ones that keep a user's other MCP servers safe):
  * never rewrite the whole file from a fresh template -- merge into what is there
  * never drop unknown keys
  * back up before every touch
  * refuse to touch a file that does not parse (report it instead of destroying it)
"""

from __future__ import annotations

import json
from pathlib import Path

from .fsutil import InstallError, backup_file, read_text_flexible, write_text_atomic


def load_json_config(path: Path) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    text = read_text_flexible(path).strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstallError(
            f"existing file is not valid JSON ({exc.msg} at line {exc.lineno}); "
            f"left untouched -- fix or delete it, then re-run"
        ) from exc
    if not isinstance(data, dict):
        raise InstallError("existing file holds a JSON array/scalar, not an object")
    return data


def dump_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def set_server(data: dict, key_path: list[str], name: str, spec: dict) -> str:
    """Insert/replace ``name`` inside the nested dict at ``key_path``.

    Returns 'created' | 'updated' | 'unchanged'.
    """
    if not key_path:
        raise ValueError("key_path must not be empty")
    node = data
    for key in key_path[:-1]:
        nxt = node.get(key)
        if nxt is None:
            nxt = {}
            node[key] = nxt
        if not isinstance(nxt, dict):
            raise InstallError(f"config key '{key}' is a {type(nxt).__name__}, expected an object")
        node = nxt
    leaf = key_path[-1]
    servers = node.get(leaf)
    if servers is None:
        servers = {}
        node[leaf] = servers
    if not isinstance(servers, dict):
        raise InstallError(f"config key '{'.'.join(key_path)}' is not an object")
    existed = name in servers
    if existed and servers[name] == spec:
        return "unchanged"
    servers[name] = spec
    return "updated" if existed else "created"


def remove_server(data: dict, key_path: list[str], name: str) -> bool:
    node = data
    for key in key_path[:-1]:
        node = node.get(key)  # type: ignore[assignment]
        if not isinstance(node, dict):
            return False
    servers = node.get(key_path[-1])
    if isinstance(servers, dict) and name in servers:
        del servers[name]
        return True
    return False


def get_server(data: dict, key_path: list[str], name: str) -> dict | None:
    node = data
    for key in key_path[:-1]:
        node = node.get(key)  # type: ignore[assignment]
        if not isinstance(node, dict):
            return None
    servers = node.get(key_path[-1])
    if isinstance(servers, dict):
        spec = servers.get(name)
        return spec if isinstance(spec, dict) else None
    return None


def detect_key_path(data: dict, candidates: list[list[str]]) -> list[str]:
    """Pick the key path already used by this file, else the first candidate.

    This is what lets us adapt to hosts that renamed the section between
    releases (``mcpServers`` vs ``mcp.servers``) without hardcoding a guess.
    """
    for path in candidates:
        node = data
        ok = True
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]  # type: ignore[assignment]
            else:
                ok = False
                break
        if ok:
            return path
    return candidates[0]


def apply_server(
    path: Path,
    key_path_candidates: list[list[str]],
    name: str,
    spec: dict,
    *,
    backup: bool = True,
) -> dict:
    """Read-merge-write a JSON host config. Returns a result record."""
    data = load_json_config(path)
    key_path = detect_key_path(data, key_path_candidates)
    action = set_server(data, key_path, name, spec)
    if action == "unchanged":
        return {
            "path": str(path), "action": "unchanged", "key_path": ".".join(key_path),
            "backup": None, "existed_before": True,
        }
    bak = backup_file(path) if backup else None
    write_text_atomic(path, dump_json(data))
    return {
        "path": str(path), "action": action, "key_path": ".".join(key_path),
        "backup": str(bak) if bak else None, "existed_before": bak is not None,
    }


def strip_server(
    path: Path,
    key_path_candidates: list[list[str]],
    name: str,
    *,
    backup: bool = True,
) -> dict:
    if not path.exists():
        return {"path": str(path), "action": "absent", "backup": None}
    data = load_json_config(path)
    key_path = detect_key_path(data, key_path_candidates)
    if not remove_server(data, key_path, name):
        return {"path": str(path), "action": "absent", "backup": None}
    bak = backup_file(path) if backup else None
    write_text_atomic(path, dump_json(data))
    return {"path": str(path), "action": "removed", "backup": str(bak) if bak else None}
