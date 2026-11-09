"""Payload staging: where the runtime files come from and where they land.

Two source layouts are supported:
  * frozen  -> a single-file exe, payload unpacked under ``sys._MEIPASS/payload``
  * source  -> a git checkout, payload read straight from the repo root
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import __version__
from .fsutil import InstallError, ensure_dir, read_text_flexible, write_text_atomic

# relative paths inside the payload, plus whether they are mandatory
PAYLOAD_FILES: list[tuple[str, bool]] = [
    ("scripts/locmodel_image.py", True),
    ("scripts/locmodel_mcp.py", True),
    ("SKILL.md", True),
    ("README.md", False),
    ("README.zh-CN.md", False),
    ("LICENSE", False),
]

CONFIG_EXAMPLE = """{
  "host": "127.0.0.1",
  "port": null,
  "output": {
    "dir": null
  },
  "generate": {
    "timeout": 900,
    "retry": 2
  }
}
"""


def payload_root() -> Path:
    """Directory that contains scripts/, SKILL.md, ... (read-only)."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        cand = base / "payload"
        if cand.is_dir():
            return cand
        raise InstallError("payload payload/ missing from the frozen bundle")
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "scripts" / "locmodel_mcp.py").is_file():
            return parent
    raise InstallError("cannot locate repo root (scripts/locmodel_mcp.py not found)")


def frozen_exe() -> Path | None:
    """Path of the running single-file exe, or None in source mode."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return None


def _same_bytes(a: Path, b: Path) -> bool:
    """Cheap content comparison for the copied installer exe."""
    import hashlib

    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        def digest(path: Path) -> str:
            h = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        return digest(a) == digest(b)
    except OSError:
        return False


def stage(install_dir: Path, *, copy_exe: bool = True) -> dict:
    """Write the runtime files into ``install_dir``. Returns a manifest dict."""
    src = payload_root()
    ensure_dir(install_dir)
    written: list[str] = []
    missing: list[str] = []

    for rel, required in PAYLOAD_FILES:
        s = src / rel
        if not s.is_file():
            if required:
                missing.append(rel)
                continue
            continue
        d = install_dir / rel
        ensure_dir(d.parent)
        write_text_atomic(d, read_text_flexible(s))
        written.append(rel)

    if missing:
        raise InstallError(f"payload incomplete, missing: {', '.join(missing)}")

    cfg_example = install_dir / "config.example.json"
    if not cfg_example.exists():
        write_text_atomic(cfg_example, CONFIG_EXAMPLE)
        written.append("config.example.json")

    exe_path = None
    exe_note = None
    if copy_exe:
        exe = frozen_exe()
        if exe:
            target = install_dir / exe.name
            if exe.resolve() != target.resolve():
                if target.is_file() and _same_bytes(exe, target):
                    exe_note = "unchanged"
                else:
                    import shutil
                    try:
                        shutil.copy2(exe, target)
                    except (PermissionError, OSError) as exc:
                        # A host client keeps the installed exe open while its MCP
                        # server is alive. Overwriting is impossible then, but the
                        # old copy still works, so downgrade to a warning.
                        if target.is_file():
                            exe_note = (
                                "in use, kept the previous build "
                                f"({type(exc).__name__})"
                            )
                        else:
                            raise InstallError(
                                f"cannot copy the installer to {target}: {exc}. "
                                "Close any agent client that is using it and retry."
                            ) from exc
            exe_path = target
            if exe_note != "unchanged":
                written.append(target.name)

    manifest = {
        "app": "locmodel-image",
        "version": __version__,
        "install_dir": str(install_dir),
        "exe": str(exe_path) if exe_path else None,
        "exe_note": exe_note,
        "files": written,
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    (install_dir / "VERSION").write_text(__version__ + "\n", encoding="utf-8")
    (install_dir / "install-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def read_manifest(install_dir: Path) -> dict | None:
    p = install_dir / "install-manifest.json"
    if not p.is_file():
        return None
    try:
        return json.loads(read_text_flexible(p))
    except Exception:
        return None


def verify(install_dir: Path) -> list[dict]:
    """Check every staged file is present and non-empty."""
    checks: list[dict] = []
    for rel, required in PAYLOAD_FILES:
        p = install_dir / rel
        ok = p.is_file() and p.stat().st_size > 0
        checks.append({"name": f"file:{rel}", "ok": ok,
                       "detail": f"{p} ({p.stat().st_size} B)" if p.is_file() else f"{p} missing",
                       "level": "error" if required else "warn"})
    m = read_manifest(install_dir)
    checks.append({"name": "manifest", "ok": m is not None,
                   "detail": str(install_dir / "install-manifest.json"), "level": "error"})
    return checks
