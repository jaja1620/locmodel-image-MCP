"""Install / verify / list / uninstall orchestration."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import MCP_SERVER_KEY, __version__
from . import hosts as hostmod
from . import jsoncfg, payload, selftest
from .fsutil import (
    InstallError, ensure_dir, python_rejected_notes, read_text_flexible,
    which_python, write_text_atomic,
)


# --------------------------------------------------------------------------
# options
# --------------------------------------------------------------------------

@dataclass
class Options:
    targets: str = "auto"                 # auto | all | csv of host ids
    mode: str = "auto"                    # auto | exe | python
    install_dir: Path | None = None
    python: str | None = None
    dry_run: bool = False
    assume_yes: bool = False
    backup: bool = True
    run_doctor: bool = True
    purge: bool = False
    config_path: Path | None = None
    skip_smoke: bool = False


def default_install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path(os.path.expanduser("~")) / "AppData" / "Local")
    return Path(base) / "locmodel-image"


def default_config_path() -> Path:
    return Path(os.path.expanduser("~")) / ".locmodel-image" / "config.json"


# --------------------------------------------------------------------------
# target selection
# --------------------------------------------------------------------------

def resolve_targets(opts: Options) -> tuple[list[hostmod.Host], list[dict]]:
    all_hosts = hostmod.build_hosts()
    skipped: list[dict] = []
    wanted = (opts.targets or "auto").strip().lower()

    if wanted in ("all", "*"):
        chosen = all_hosts
    elif wanted == "auto":
        chosen = []
        for h in all_hosts:
            if h.kind == "manual":
                if h.id in hostmod.MANUAL_HOSTS:
                    chosen.append(h)
                continue
            if h.is_installed_software():
                chosen.append(h)
            else:
                skipped.append({"host": h.id, "reason": "not installed on this machine"})
    else:
        ids = [t.strip() for t in wanted.replace(";", ",").split(",") if t.strip()]
        bad = [i for i in ids if hostmod.find_host(all_hosts, i) is None]
        if bad:
            raise InstallError(
                f"unknown host id(s): {', '.join(bad)}; valid: {', '.join(h.id for h in all_hosts)}"
            )
        chosen = [hostmod.find_host(all_hosts, i) for i in ids]  # type: ignore[list-item]

    if opts.dry_run is False and wanted == "all":
        skipped.clear()
    return chosen, skipped


# --------------------------------------------------------------------------
# launch spec
# --------------------------------------------------------------------------

def build_launch(opts: Options, install_dir: Path, staged: dict,
                 allow_pending: bool = False) -> dict:
    mode = opts.mode
    if mode == "auto":
        mode = "exe" if staged.get("exe") else "python"

    if mode == "exe":
        exe = staged.get("exe")
        if not exe:
            raise InstallError(
                "exe mode needs the frozen single-file build; run the packaged "
                "locmodel-image-setup.exe, or pass --mode python"
            )
        command = Path(exe)
        args = ["--serve"]
    else:
        py = opts.python or which_python()
        if not py and allow_pending:
            notes = python_rejected_notes()
            return {
                "mode": "python",
                "command": "",
                "args": [str(install_dir / "scripts" / "locmodel_mcp.py")],
                "display": "<未找到独立的 CPython 3.8+，需 --python 指定或改用 exe 模式>",
                "pending": True,
                "warning": ("python 模式找不到独立解释器"
                            + (f"（{'；'.join(notes)}）" if notes else "")
                            + "；打包为 exe 后会自动改用 exe 模式"),
            }
        if not py:
            notes = python_rejected_notes()
            detail = ("；".join(notes) if notes else "PATH 上没有 CPython 3.8+")
            raise InstallError(
                "python 模式需要一个独立的 CPython 3.8+（" + detail + "）。"
                "推荐改用打包好的单文件 exe：它自身即为 MCP server，目标机无需 Python。"
            )
        command = Path(py)
        args = [str(install_dir / "scripts" / "locmodel_mcp.py")]

    return {
        "mode": mode,
        "command": str(command),
        "args": args,
        "display": " ".join([f'"{command}"', *args]),
    }


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------

def install(opts: Options) -> dict:
    install_dir = Path(opts.install_dir) if opts.install_dir else default_install_dir()
    report: dict = {
        "ok": False,
        "version": __version__,
        "install_dir": str(install_dir),
        "mode": None,
        "targets": [],
        "skipped": [],
        "checks": [],
        "warnings": [],
        "next_steps": [],
    }

    chosen, skipped = resolve_targets(opts)
    report["skipped"] = skipped
    if not chosen:
        report["warnings"].append("no host detected -- nothing to register")
        return report

    if opts.dry_run:
        try:
            launch = build_launch(opts, install_dir, {"exe": payload.frozen_exe()},
                                  allow_pending=True)
            if launch.get("warning"):
                report["warnings"].append(launch["warning"])
        except InstallError as exc:
            launch = {"mode": opts.mode, "command": "", "args": [],
                      "display": f"<不可用: {exc}>"}
            report["warnings"].append(str(exc))
        report["mode"] = launch["mode"]
        report["launch"] = launch
        for h in chosen:
            path, status = h.resolved_path()
            report["targets"].append({"host": h.id, "label": h.label, "status": status,
                                      "path": str(path), "action": "would-write"})
        report["ok"] = True
        return report

    # 1. stage files
    try:
        staged = payload.stage(install_dir, copy_exe=True)
    except InstallError as exc:
        report["checks"].append({"name": "payload.stage", "ok": False, "level": "error",
                                 "detail": str(exc)})
        return report
    report["checks"].append({"name": "payload.stage", "ok": True, "level": "error",
                             "detail": f"{len(staged['files'])} files -> {install_dir}"})
    report["checks"].extend(payload.verify(install_dir))

    exe_note = (staged or {}).get("exe_note")
    if exe_note and exe_note != "unchanged":
        report["warnings"].append(
            "程序文件正被宿主占用，本次保留安装目录中的旧版本"
            f"（{exe_note}）。关闭宿主后重新运行安装器即可完成更新。"
        )

    ensure_dir(install_dir / "output")

    # 2. launch spec
    try:
        launch = build_launch(opts, install_dir, staged)
    except InstallError as exc:
        report["checks"].append({"name": "launch.spec", "ok": False, "level": "error",
                                 "detail": str(exc)})
        report["next_steps"] = [
            "改用 exe 模式重装（目标机无需 Python）: " 
            f'"{Path(staged.get("exe") or "locmodel-image-setup.exe").name} install --mode exe"',
            "或先安装独立 CPython 3.8+，再用 --python <解释器路径> 指定",
        ]
        return report
    report["mode"] = launch["mode"]
    report["launch"] = launch
    report["checks"].append({"name": "launch.spec", "ok": True, "level": "error",
                             "detail": launch["display"]})

    # 3. seed the runtime config (never overwrite an existing one)
    cfg_path = Path(opts.config_path) if opts.config_path else default_config_path()
    try:
        seeded = _seed_config(cfg_path, install_dir)
        report["checks"].append({"name": "runtime.config", "ok": True, "level": "info",
                                 "detail": f"{cfg_path} ({seeded})"})
    except Exception as exc:
        report["checks"].append({"name": "runtime.config", "ok": False, "level": "warn",
                                 "detail": f"{cfg_path}: {exc}"})

    # 4. register with each host
    for h in chosen:
        if h.kind == "manual":
            report["targets"].append({
                "host": h.id, "label": h.label, "status": "manual",
                "reason": "registered via the agent itself, not a config file",
            })
            continue
        try:
            res = h.apply(launch["command"], launch["args"], backup=opts.backup)
        except Exception as exc:
            res = {"host": h.id, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
        res["label"] = h.label
        report["targets"].append(res)

    written = [t for t in report["targets"] if t.get("status") in ("created", "updated")]
    if written and launch["mode"] == "python":
        report["warnings"].append(
            "python mode: the host config points at an absolute interpreter path; "
            "if that Python is moved or removed the server stops loading"
        )

    # 5. prove it works
    if not opts.skip_smoke:
        smoke = selftest.mcp_smoke(
            [launch["command"], *launch["args"]],
            cwd=install_dir,
            call_doctor=opts.run_doctor,
            timeout=60.0,
        )
        for c in smoke["checks"]:
            report["checks"].append({**c, "name": "smoke." + c["name"]})
        report["smoke_ok"] = smoke["ok"]

    # 6. verdict
    hard = [c for c in report["checks"] if c.get("level") == "error" and not c["ok"]]
    failed_hosts = [t for t in report["targets"] if t.get("status") == "failed"]
    report["ok"] = not hard and not failed_hosts
    if any(not c["ok"] for c in report["checks"] if c["name"] == "mcp.initialize"):
        report["ok"] = False

    report["next_steps"] = _next_steps(report, install_dir)
    write_report(report, install_dir)
    return report


def _seed_config(cfg_path: Path, install_dir: Path) -> str:
    if cfg_path.exists():
        return "kept existing"
    data = {"host": "127.0.0.1", "port": None, "output": {"dir": str(install_dir / "output")}}
    write_text_atomic(cfg_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return "created"


def _next_steps(report: dict, install_dir: Path) -> list[str]:
    steps: list[str] = []
    launch_cmd = ((report.get("launch") or {}).get("command") or "").strip()
    exe_name = Path(launch_cmd).name if launch_cmd else "locmodel-image-setup.exe"

    live = [t for t in report.get("targets") or [] if t.get("status") in ("created", "updated")]
    if live:
        steps.append(
            "重启宿主客户端以重新加载 MCP 配置: "
            + ", ".join(t.get("label") or t["host"] for t in live)
        )
    if any(t.get("host") == "marvis" for t in (report.get("targets") or [])):
        steps.append(
            f'在 Marvis 对话里粘贴注册指令完成接入: "{exe_name} emit marvis"'
        )
    engine = next((c for c in (report.get("checks") or [])
                   if c["name"].endswith("engine.doctor")), None)
    if engine and not engine["ok"]:
        steps.append(
            "画图引擎当前未就绪: 启动 LocModel 并载入模型后，再执行 "
            f'"{exe_name} doctor" 复检（安装本身已成功，引擎可稍后启动）'
        )
    steps.append(f'卸载: "{exe_name} uninstall"（加 --purge 可同时删除程序文件）')
    return steps


def write_report(report: dict, install_dir: Path) -> Path | None:
    try:
        ensure_dir(install_dir)
        p = install_dir / "last-install-report.json"
        write_text_atomic(p, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return p
    except Exception:
        return None


# --------------------------------------------------------------------------
# list / doctor / uninstall
# --------------------------------------------------------------------------

def list_state(opts: Options) -> dict:
    all_hosts = hostmod.build_hosts()
    rows = []
    for h in all_hosts:
        path, status = h.resolved_path()
        entry = h.current_entry()
        registered = entry is not None
        detail = entry.get("command", "") if entry else ""
        rows.append({
            "host": h.id, "label": h.label, "detected": status,
            "path": str(path), "registered": registered,
            "command": detail,
            "args": entry.get("args") if entry else None,
        })
    install_dir = Path(opts.install_dir) if opts.install_dir else default_install_dir()
    return {
        "ok": True,
        "version": __version__,
        "install_dir": str(install_dir),
        "installed": (install_dir / "install-manifest.json").is_file(),
        "runtime_config": str(default_config_path()),
        "hosts": rows,
    }


def doctor(opts: Options) -> dict:
    install_dir = Path(opts.install_dir) if opts.install_dir else default_install_dir()
    checks: list[dict] = []

    manifest = payload.read_manifest(install_dir)
    checks.append({"name": "install.manifest", "ok": manifest is not None, "level": "error",
                   "detail": str(install_dir / "install-manifest.json")})
    checks.extend(payload.verify(install_dir))

    cfg_path = Path(opts.config_path) if opts.config_path else default_config_path()
    checks.append({"name": "runtime.config", "ok": True, "level": "info",
                   "detail": f"{cfg_path} {'exists' if cfg_path.exists() else 'defaults'}"})

    launch = None
    if manifest:
        mode = "exe" if manifest.get("exe") and Path(manifest["exe"]).exists() else "python"
        if mode == "exe":
            launch = {"mode": "exe", "command": manifest["exe"], "args": ["--serve"]}
        else:
            py = opts.python or which_python()
            script = install_dir / "scripts" / "locmodel_mcp.py"
            if py and script.is_file():
                launch = {"mode": "python", "command": py, "args": [str(script)]}
        if launch:
            smoke = selftest.mcp_smoke([launch["command"], *launch["args"]],
                                       cwd=install_dir, call_doctor=opts.run_doctor, timeout=60.0)
            for c in smoke["checks"]:
                checks.append({**c, "name": "smoke." + c["name"]})
            launch["display"] = " ".join([launch["command"], *launch["args"]])
        else:
            checks.append({"name": "smoke", "ok": False, "level": "error",
                           "detail": "no runnable entry point (exe missing and no Python found)"})

    registry = []
    for h in hostmod.build_hosts():
        if h.kind == "manual":
            continue
        entry = h.current_entry()
        if entry is None:
            continue
        expected = launch["command"] if launch else None
        registry.append({
            "name": f"hosts.{h.id}",
            "ok": expected is None or entry.get("command") == expected,
            "level": "info",
            "detail": f"{h.resolved_path()[0]} -> {entry.get('command')} {entry.get('args')}",
        })
    checks.extend(registry)

    ok = all(c["ok"] for c in checks if c.get("level") == "error")
    return {"ok": ok, "version": __version__, "install_dir": str(install_dir),
            "launch": launch, "checks": checks}


def _locked_exe(install_dir: Path) -> Path | None:
    """Return the installed exe when another process still keeps it open.

    A frozen exe cannot delete itself while a host client is running it, so we
    probe before touching anything instead of failing halfway through.
    """
    for exe in sorted(install_dir.glob("*.exe")):
        try:
            with exe.open("r+b"):
                pass
        except OSError:
            return exe
    return None


def uninstall(opts: Options) -> dict:
    install_dir = Path(opts.install_dir) if opts.install_dir else default_install_dir()
    result: dict = {"ok": True, "install_dir": str(install_dir), "hosts": [], "purged": False}

    for h in hostmod.build_hosts():
        if h.kind == "manual":
            continue
        try:
            result["hosts"].append({**h.strip(backup=opts.backup), "label": h.label})
        except Exception as exc:
            result["hosts"].append({"host": h.id, "status": "failed",
                                    "reason": f"{type(exc).__name__}: {exc}"})

    if opts.purge:
        if install_dir.exists():
            locked = _locked_exe(install_dir)
            for _ in range(3):
                if locked is None:
                    break
                # a host that was just closed may still be releasing the file
                time.sleep(1.5)
                locked = _locked_exe(install_dir)
            if locked is not None:
                result["ok"] = False
                result["note"] = (
                    f"程序文件仍被占用，未删除任何文件：{locked}。"
                    "宿主的 MCP 进程可能仍在后台运行：请关闭宿主客户端，"
                    "或在任务管理器里结束 locmodel-image-setup.exe 后，"
                    "重新执行 uninstall --purge。"
                )
                return result
            try:
                shutil.rmtree(install_dir)
                result["purged"] = True
            except Exception as exc:
                result["ok"] = False
                result["purge_error"] = (
                    f"{type(exc).__name__}: {exc}. "
                    "关闭正在使用该目录的宿主（其 MCP 进程可能仍在运行）后重试。"
                )
    else:
        result["note"] = (
            f"runtime files kept in {install_dir}; user config in {default_config_path()} "
            "and generated images were not touched (re-run with --purge to delete the program files)"
        )
    result["ok"] = result["ok"] and not any(
        h.get("status") == "failed" for h in result["hosts"]
    )
    return result


# --------------------------------------------------------------------------
# manual-host instructions (Marvis and friends)
# --------------------------------------------------------------------------

def emit_manual(opts: Options, host_id: str = "marvis") -> str:
    install_dir = Path(opts.install_dir) if opts.install_dir else default_install_dir()
    manifest = payload.read_manifest(install_dir)
    exe = (manifest or {}).get("exe")
    if exe and Path(exe).exists():
        command, args = exe, ["--serve"]
        env_note = ""
    else:
        py = opts.python or which_python() or "python"
        command, args = py, [str(install_dir / "scripts" / "locmodel_mcp.py")]
        env_note = " (python mode)"

    cfg = {
        "command": command,
        "args": args,
        "env": {"LOCMODEL_IMAGE_CONFIG": str(Path(opts.config_path) if opts.config_path else default_config_path())},
    }
    skill_dir = install_dir / "SKILL.md"

    if host_id == "marvis":
        return (
            "【Marvis 安装指令 · 直接粘贴到对话里发给 Marvis】\n"
            "帮我把本机的 locmodel-image 画图能力接上：\n"
            f"1) 用 marvis_capabilities 的 mcp_install 注册一个私有 MCP server，"
            f'name 用 "{MCP_SERVER_KEY}"，config 用下面这段：\n'
            f"{json.dumps(cfg, ensure_ascii=False, indent=2)}\n"
            f"2) 用 skill_install 的 local_paths 安装这个 Skill 目录：{skill_dir.parent}\n"
            "3) 装完我用「画一张……」测试。\n"
        )

    return (
        f"【{host_id} 手动注册】\n"
        f"config:\n{json.dumps(cfg, ensure_ascii=False, indent=2)}\n" + env_note
    )
