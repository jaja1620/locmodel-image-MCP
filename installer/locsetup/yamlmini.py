"""Very small YAML editor -- only what is needed for host config files.

Why not use PyYAML: the installer must stay dependency-free. Why not regex-replace:
because a round-trip through a real parser would reformat the user's whole file
(comments, ordering, quoting) and that is unacceptable for someone's agent config.

So we do a *line-level* insertion inside one existing top-level section, then
verify the result: with PyYAML when it happens to be importable, otherwise with
our own structural reader. If verification fails the caller restores the backup.
"""

from __future__ import annotations

import re
from pathlib import Path

from .fsutil import InstallError, backup_file, read_text_flexible, write_text_atomic


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _top_level_sections(lines: list[str]) -> list[tuple[str, int, int, int]]:
    """Return (name, header_index, body_start, body_end) for each top-level key."""
    out: list[tuple[str, int, int, int]] = []
    for i, line in enumerate(lines):
        if _is_blank(line) or _indent_of(line) != 0:
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*:\s*(#.*)?$", line.rstrip("\n"))
        if not m:
            continue
        start = i + 1
        end = len(lines)
        for j in range(start, len(lines)):
            if _is_blank(lines[j]):
                continue
            if _indent_of(lines[j]) == 0:
                end = j
                break
        out.append((m.group(1), i, start, end))
    return out


def find_section(text: str, names: list[str]) -> str | None:
    """Return the first section name present at top level, tolerating a missing colon."""
    lines = text.splitlines()
    present = [s[0] for s in _top_level_sections(lines)]
    for name in names:
        if name in present:
            return name
    return None


def read_section_names(text: str) -> list[str]:
    return [s[0] for s in _top_level_sections(text.splitlines())]


def _child_indent(lines: list[str], body_start: int, body_end: int) -> int:
    for i in range(body_start, body_end):
        if not _is_blank(lines[i]):
            ind = _indent_of(lines[i])
            return ind if ind > 0 else 2
    return 2


def _yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or re.search(r"[:#\{\}\[\],&*!|>'\"%@`]", text) or text != text.strip():
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _render_entry(name: str, spec: dict, indent: int) -> list[str]:
    pad = " " * indent
    inner = " " * (indent + 2)
    lines = [f"{pad}{name}:"]
    for key, value in spec.items():
        if isinstance(value, dict):
            lines.append(f"{inner}{key}:")
            for k2, v2 in value.items():
                if isinstance(v2, (dict, list)):
                    import json as _json
                    lines.append(f"{inner}  {k2}: {json_inline(v2)}")
                else:
                    lines.append(f"{inner}  {k2}: {_yaml_scalar(v2)}")
        elif isinstance(value, (list, dict)):
            lines.append(f"{inner}{key}: {json_inline(value)}")
        else:
            lines.append(f"{inner}{key}: {_yaml_scalar(value)}")
    return lines


def json_inline(value) -> str:
    """JSON is valid YAML flow syntax, so lists/dicts can be emitted as JSON."""
    import json as _json
    return _json.dumps(value, ensure_ascii=False)


def upsert_section_entry(text: str, section: str, name: str, spec: dict) -> str:
    """Insert or replace ``name`` under top-level ``section``; return new text."""
    lines = text.splitlines()
    sections = _top_level_sections(lines)

    target = next((s for s in sections if s[0] == section), None)
    if target is None:
        if text and not text.endswith("\n"):
            text += "\n"
        block = _render_entry(name, spec, 2)
        return text + f"{section}:\n" + "\n".join(block) + "\n"

    _, _, body_start, body_end = target
    indent = _child_indent(lines, body_start, body_end)
    child_indent = indent

    # locate an existing entries with this name at child indent
    entry_start = None
    for i in range(body_start, body_end):
        if _is_blank(lines[i]):
            continue
        if _indent_of(lines[i]) == child_indent and re.match(
            rf"^\s*{re.escape(name)}\s*:\s*$", lines[i]
        ):
            entry_start = i
            break
        if _indent_of(lines[i]) == child_indent and re.match(
            rf"^\s*{re.escape(name)}\s*:\s*\S", lines[i]
        ):
            entry_start = i
            break

    block = _render_entry(name, spec, child_indent)

    if entry_start is None:
        insert_at = body_end
        # keep trailing blank/comment lines of the file outside the section
        while insert_at > body_start and _is_blank(lines[insert_at - 1]):
            insert_at -= 1
        new_lines = lines[:insert_at] + block + lines[insert_at:]
    else:
        entry_end = body_end
        for j in range(entry_start + 1, body_end):
            if _is_blank(lines[j]):
                continue
            if _indent_of(lines[j]) <= child_indent:
                entry_end = j
                break
        new_lines = lines[:entry_start] + block + lines[entry_end:]

    return "\n".join(new_lines) + "\n"


def _verify_with_pyyaml(text: str, section: str, name: str) -> tuple[bool, str]:
    try:
        import yaml  # type: ignore
    except Exception:
        return True, "skipped (PyYAML not installed)"
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        return False, f"PyYAML parse failed: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get(section), dict):
        return False, f"section '{section}' missing after write"
    if name not in data[section]:
        return False, f"entry '{name}' missing after write"
    return True, "PyYAML ok"


def _verify_structurally(text: str, section: str, name: str) -> tuple[bool, str]:
    lines = text.splitlines()
    for sec, _, body_start, body_end in _top_level_sections(lines):
        if sec != section:
            continue
        for i in range(body_start, body_end):
            if re.match(rf"^\s*{re.escape(name)}\s*:", lines[i]):
                return True, "structural ok"
        return False, f"entry '{name}' not found under '{section}'"
    return False, f"section '{section}' not found"


def _normalised(text: str) -> str:
    """Compare-for-equality helper: ignore CRLF, trailing blanks and trailing newline."""
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()


def apply_server(path: Path, section_candidates: list[str], name: str, spec: dict,
                 *, backup: bool = True) -> dict:
    text = read_text_flexible(path) if path.exists() else ""
    section = find_section(text, section_candidates) if text else None
    appended_section = False
    if section is None:
        # No known section yet. Appending a *new top-level* key is safe: it never
        # rewrites existing keys, and YAML allows the new block at end of file.
        # The result is still validated below (PyYAML when available, else a
        # structural check), and the original file is backed up first.
        section = section_candidates[0]
        if text.strip():
            appended_section = True
            if not text.endswith("\n"):
                text += "\n"
            if not text.endswith("\n\n"):
                text += "\n"

    new_text = upsert_section_entry(text, section, name, spec)
    ok1, why1 = _verify_with_pyyaml(new_text, section, name)
    if ok1 and "skipped" in why1:
        ok1, why1 = _verify_structurally(new_text, section, name)
    if not ok1:
        bak = backup_file(path) if (backup and path.exists()) else None
        return {
            "path": str(path), "action": "failed", "backup": str(bak) if bak else None,
            "reason": f"verification failed: {why1} (backup kept)",
        }

    if path.exists() and _normalised(read_text_flexible(path)) == _normalised(new_text):
        # Nothing to change: report it so a re-run looks like a no-op.
        return {"path": str(path), "action": "unchanged", "section": section,
                "backup": None, "reason": "already configured"}
    bak = backup_file(path) if backup else None
    write_text_atomic(path, new_text)
    result = {"path": str(path), "action": "updated", "section": section,
              "backup": str(bak) if bak else None, "reason": why1}
    if appended_section:
        result["note"] = "appended a new top-level section"
    return result


def strip_server(path: Path, section_candidates: list[str], name: str,
                 *, backup: bool = True) -> dict:
    if not path.exists():
        return {"path": str(path), "action": "absent", "backup": None}
    text = read_text_flexible(path)
    section = find_section(text, section_candidates)
    if section is None:
        return {"path": str(path), "action": "absent", "backup": None}

    lines = text.splitlines()
    sections = _top_level_sections(lines)
    target = next((s for s in sections if s[0] == section), None)
    if not target:
        return {"path": str(path), "action": "absent", "backup": None}
    _, _, body_start, body_end = target
    child_indent = _child_indent(lines, body_start, body_end)
    start = None
    for i in range(body_start, body_end):
        if _indent_of(lines[i]) == child_indent and re.match(
            rf"^\s*{re.escape(name)}\s*:", lines[i]
        ):
            start = i
            break
    if start is None:
        return {"path": str(path), "action": "absent", "backup": None}
    end = body_end
    for j in range(start + 1, body_end):
        if _is_blank(lines[j]):
            continue
        if _indent_of(lines[j]) <= child_indent:
            end = j
            break
    new_lines = lines[:start] + lines[end:]
    bak = backup_file(path) if backup else None
    write_text_atomic(path, "\n".join(new_lines) + "\n")
    return {"path": str(path), "action": "removed", "backup": str(bak) if bak else None}
