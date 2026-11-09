"""Command line surface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import core, payload, ui
from .fsutil import InstallError


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dir", dest="install_dir", metavar="PATH",
                   help="安装目录 (默认 %%LOCALAPPDATA%%\\locmodel-image)")
    p.add_argument("--python", metavar="PATH", help="python 模式的解释器路径")
    p.add_argument("--config", dest="config_path", metavar="PATH",
                   help="运行时 config.json 路径 (默认 ~/.locmodel-image/config.json)")
    p.add_argument("--no-backup", action="store_true", help="写入前不创建 .bak 备份（不建议）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    p.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="locmodel-image-setup",
        description="locmodel-image 一键安装器：部署运行时、注册 MCP 宿主、自检；双击即可安装。",
    )
    ap.add_argument("--version", action="version", version=f"locmodel-image-setup {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    p_inst = sub.add_parser("install", help="安装并注册到检测到的宿主")
    _add_common(p_inst)
    p_inst.add_argument("--targets", default="auto", metavar="auto|all|CSV",
                        help="auto=仅已安装的宿主, all=全部, 或逗号分隔的宿主 id")
    p_inst.add_argument("--mode", default="auto", choices=["auto", "exe", "python"],
                        help="exe=由安装器自身兼任 MCP server (目标机无需 Python)")
    p_inst.add_argument("--dry-run", action="store_true", help="只打印计划，不写任何文件")
    p_inst.add_argument("--skip-engine", action="store_true", help="自检时不探测画图引擎")
    p_inst.add_argument("--skip-smoke", action="store_true", help="跳过 MCP 握手自检")

    p_doc = sub.add_parser("doctor", help="自检：文件完整性 + MCP 握手 + 引擎")
    _add_common(p_doc)
    p_doc.add_argument("--skip-engine", action="store_true")

    p_list = sub.add_parser("list", help="列出宿主检测与注册状态")
    _add_common(p_list)

    p_un = sub.add_parser("uninstall", help="从宿主配置中移除，可选删除程序文件")
    _add_common(p_un)
    p_un.add_argument("--purge", action="store_true", help="同时删除安装目录（保留用户配置与出图）")

    p_emit = sub.add_parser("emit", help="打印手动注册指令（Marvis 等无文件配置的宿主）")
    _add_common(p_emit)
    p_emit.add_argument("host", nargs="?", default="marvis",
                        choices=["marvis", "manual"])
    p_emit.add_argument("--host", dest="host_opt", choices=["marvis", "manual"],
                        help="与位置参数 host 等价，便于脚本调用")

    sub.add_parser("hosts", help="列出所有支持的宿主 id")
    sub.add_parser("serve", help="以 MCP stdio 服务方式运行（供宿主调用）")
    return ap


def _opts(args) -> core.Options:
    return core.Options(
        targets=getattr(args, "targets", "auto"),
        mode=getattr(args, "mode", "auto"),
        install_dir=Path(args.install_dir).expanduser() if getattr(args, "install_dir", None) else None,
        python=getattr(args, "python", None),
        dry_run=getattr(args, "dry_run", False),
        assume_yes=getattr(args, "yes", False),
        backup=not getattr(args, "no_backup", False),
        run_doctor=not getattr(args, "skip_engine", False),
        purge=getattr(args, "purge", False),
        config_path=Path(args.config_path).expanduser() if getattr(args, "config_path", None) else None,
        skip_smoke=getattr(args, "skip_smoke", False),
    )


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=True, indent=2))


def run(argv: list[str]) -> int:
    if not argv:
        argv = ["install"]
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        return serve()

    if args.cmd == "hosts":
        for h in core.hostmod.build_hosts():
            print(f"{h.id:<16} {h.kind:<7} {h.label}")
            for p in h.paths:
                print(f"{'':<25}{p}")
            if h.notes:
                print(f"{'':<25}{h.notes}")
        return 0

    opts = _opts(args)

    try:
        if args.cmd == "install":
            return _cmd_install(opts, args)
        if args.cmd == "doctor":
            res = core.doctor(opts)
            if args.json:
                _dump(res)
            else:
                ui.render_check_block(f"locmodel-image {__version__} · 自检", res)
            return 0 if res["ok"] else 1
        if args.cmd == "list":
            res = core.list_state(opts)
            _dump(res) if args.json else ui.render_list(res)
            return 0
        if args.cmd == "uninstall":
            if not args.yes and sys.stdin.isatty():
                print("即将从宿主配置中移除 locmodel-image 的 MCP 条目"
                      + ("，并删除安装目录" if opts.purge else ""))
                try:
                    answer = input("继续? [y/N] ").strip().lower()
                except EOFError:
                    answer = ""      # no interactive stdin: fall back to the default (no)
                    print()
                if answer not in ("y", "yes"):
                    print("已取消。")
                    return 1
            res = core.uninstall(opts)
            _dump(res) if args.json else ui.render_uninstall(res)
            return 0 if res["ok"] else 1
        if args.cmd == "emit":
            host = getattr(args, "host_opt", None) or args.host
            text = core.emit_manual(opts, host)
            if args.json:
                _dump({"host": host, "text": text})
            else:
                print(text)
            return 0
    except InstallError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130

    ap.print_help()
    return 2


def _cmd_install(opts: core.Options, args) -> int:
    plan = core.install(core.Options(**{**opts.__dict__, "dry_run": True}))
    targets = [t for t in plan.get("targets") or [] if t.get("status") != "absent"]

    if opts.dry_run:
        if args.json:
            _dump(plan)
        else:
            ui.header(f"locmodel-image {__version__} · 安装预演 (dry-run)")
            print(f"  安装目录: {plan['install_dir']}")
            print(f"  启动模式: {plan.get('mode')}")
            la = plan.get("launch") or {}
            if la.get("display"):
                print(f"  启动命令: {la['display']}")
            labels = {"present": "已配置", "creatable": "新建配置"}
            print("\n  将写入以下宿主:")
            if not targets:
                print("    (无)")
            for t in targets:
                print(f"    {t['label']}  [{labels.get(t['status'], t['status'])}]")
                print(f"        {t['path']}")
            for sk in plan.get("skipped") or []:
                print(f"    {sk['host']}  [跳过] {sk['reason']}")
            for w in plan.get("warnings") or []:
                print(f"    注意: {w}")
            print("\n  未写入任何文件。")
            print()
        return 0

    if not opts.assume_yes and sys.stdin.isatty():
        live = [t for t in targets if t["status"] in ("present", "creatable")]
        print(f"安装目录: {plan['install_dir']}")
        print(f"启动模式: {plan.get('mode')}")
        print(f"将向 {len(live)} 个宿主写入 MCP 配置（原有条目会被保留，写入前自动备份）：")
        for t in live:
            print(f"  - {t['label']}: {t['path']}")
        try:
            answer = input("继续? [Y/n] ").strip().lower()
        except EOFError:
            answer = ""          # no interactive stdin: fall back to the default (yes)
            print()
        if answer not in ("", "y", "yes"):
            print("已取消。")
            return 1

    report = core.install(opts)
    if args.json:
        _dump(report)
    else:
        ui.render_install(report)
    return 0 if report.get("ok") else 1


# --------------------------------------------------------------------------
# MCP stdio entry -- this is what host clients actually launch
# --------------------------------------------------------------------------

def serve() -> int:
    """Run the bundled MCP server. NOTHING may be printed to stdout here."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    candidates: list[Path] = []
    try:
        candidates.append(payload.payload_root() / "scripts")
    except Exception:
        pass
    candidates.append(Path(sys.executable).resolve().parent / "scripts")
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "payload" / "scripts")
    candidates.append(Path(__file__).resolve().parents[2] / "scripts")

    for cand in candidates:
        if (cand / "locmodel_mcp.py").is_file():
            sys.path.insert(0, str(cand))
            try:
                import locmodel_mcp  # type: ignore
            except Exception as exc:
                sys.stderr.write(f"locmodel-image: cannot import locmodel_mcp: {exc}\n")
                return 2
            return locmodel_mcp.main()

    sys.stderr.write(
        "locmodel-image: scripts/locmodel_mcp.py not found next to this executable; "
        "re-run the installer\n"
    )
    return 2


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(run(sys.argv[1:]))
