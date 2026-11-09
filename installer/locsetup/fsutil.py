"""Filesystem helpers: atomic writes, timestamped backups, safe copies."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path


class InstallError(RuntimeError):
    """Recoverable installation failure."""


def timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_file(path: Path, tag: str = "bak") -> Path | None:
    """Copy ``path`` next to itself with a timestamp suffix. Returns the copy."""
    if not path.exists():
        return None
    dst = path.with_name(f"{path.name}.{tag}.{timestamp()}")
    n = 1
    while dst.exists():  # sub-second collisions
        dst = path.with_name(f"{path.name}.{tag}.{timestamp()}-{n}")
        n += 1
    shutil.copy2(path, dst)
    return dst


def write_text_atomic(path: Path, text: str) -> Path:
    """Write text via temp file + os.replace so a crash never truncates config."""
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        finally:
            pass
        raise
    return path


def read_text_flexible(path: Path) -> str:
    """Read UTF-8 (with or without BOM), falling back to replacement chars."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def copy_tree(src: Path, dst: Path, skip_names: tuple[str, ...] = ()) -> list[Path]:
    """Copy regular files (not dirs) one level at a time, returning written paths."""
    written: list[Path] = []
    ensure_dir(dst)
    for item in sorted(src.iterdir()):
        if item.name in skip_names or item.name == "__pycache__":
            continue
        target = dst / item.name
        if item.is_dir():
            written.extend(copy_tree(item, target, skip_names))
        elif item.is_file():
            ensure_dir(target.parent)
            shutil.copy2(item, target)
            written.append(target)
    return written


_PRIVATE_MARKERS = (
    "\\runtime\\python", "\\marvis\\", "\\tencent\\",
    "\\program files\\tencent\\", "\\appdata\\local\\temp\\_mei",
)
_STORE_MARKERS = ("\\windowsapps\\",)


def _reject_reason(path: str) -> str | None:
    """Why a python.exe cannot be pinned into a host config (None = acceptable)."""
    low = path.replace("/", "\\").lower()
    for m in _PRIVATE_MARKERS:
        if m in low:
            return "属于其它应用的私有运行时，不作为通用解释器使用"
    for m in _STORE_MARKERS:
        if m in low:
            return "是 Microsoft Store 的应用别名，路径由系统托管，不作为通用解释器使用"
    return None


def which_python() -> str | None:
    """Locate a *standalone* CPython 3.8+ for the optional python mode.

    Interpreters that live inside another application's private runtime
    (``...\\Marvis\\...\\runtime\\python311\\python.exe`` and friends) are rejected:
    pinning a host config to a path that an unrelated app owns breaks silently
    the next time that app updates.
    """
    import subprocess
    import sys

    home = Path.home()
    def is_private(path: str) -> bool:
        return _reject_reason(path) is not None

    candidates: list[str] = []
    if not getattr(sys, "frozen", False):
        candidates.append(sys.executable)
    for name in ("python.exe", "python3.exe", "python"):
        found = shutil.which(name)
        if found and found not in candidates:
            candidates.append(found)
    # common per-user install locations
    for pat in ("Programs/Python/Python3*/python.exe",
                "AppData/Local/Programs/Python/Python3*/python.exe"):
        for p in sorted(home.glob(pat), reverse=True):
            s = str(p)
            if s not in candidates:
                candidates.append(s)

    for exe in candidates:
        if is_private(exe):
            continue
        try:
            out = subprocess.run(
                [exe, "-c", "import sys;C=sys.version_info;print(C[0],C[1])"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            continue
        if out.returncode != 0:
            continue
        try:
            major, minor = (int(x) for x in out.stdout.split()[:2])
        except ValueError:
            continue
        if (major, minor) >= (3, 8):
            return exe
    return None


def python_rejected_notes() -> list[str]:
    """Human-readable reasons no standalone interpreter was found."""
    notes: list[str] = []
    for name in ("python.exe", "python3.exe", "python"):
        found = shutil.which(name)
        if not found:
            continue
        reason = _reject_reason(found)
        if reason:
            notes.append(f"{found} {reason}")
    return notes
