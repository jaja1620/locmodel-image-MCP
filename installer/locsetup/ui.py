"""Console rendering. Plain text, no emoji, no colour -- readable in CI logs too."""

from __future__ import annotations

import sys
from pathlib import Path

OK = "OK  "
WARN = "WARN"
FAIL = "FAIL"
INFO = "--  "


def _mark(level: str, ok: bool) -> str:
    if ok:
        return OK
    return FAIL if level == "error" else WARN


def header(text: str) -> None:
    print()
    print(text)
    print("-" * max(28, min(len(text) * 2, 64)))


def step(index: int, total: int, title: str) -> None:
    print(f"\n[{index}/{total}] {title}")


def line(name: str, value: str = "", mark: str = "", width: int = 22) -> None:
    pad = name.ljust(width)
    if mark:
        print(f"  {mark} {pad} {value}".rstrip())
    else:
        print(f"  {pad} {value}".rstrip())


def ok(name: str, value: str = "", width: int = 22) -> None:
    line(name, value, OK, width)


def warn(name: str, value: str = "", width: int = 22) -> None:
    line(name, value, WARN, width)


def fail(name: str, value: str = "", width: int = 22) -> None:
    line(name, value, FAIL, width)


def info(name: str, value: str = "", width: int = 22) -> None:
    line(name, value, INFO, width)


def render_install(report: dict, *, verbose: bool = True) -> None:
    header(f"locmodel-image {report.get('version')} · 一键安装器")
    print(f"  安装目录: {report.get('install_dir')}")
    print(f"  启动方式: {report.get('mode') or '-'}")

    targets = report.get("targets") or []
    if targets:
        step(1, 3, "宿主注册")
        for t in targets:
            label = t.get("label") or t.get("host") or "?"
            status = t.get("status")
            path = t.get("path") or ""
            if status in ("created", "updated", "unchanged"):
                detail = f"{path}" if status != "unchanged" else f"{path} (already up to date)"
                ok(label, detail, width=24)
            elif status == "manual":
                info(label, t.get("reason", "manual"), width=24)
            elif status == "skipped":
                info(label, t.get("reason", ""), width=24)
            else:
                fail(label, f"{t.get('reason', status)}", width=24)

    checks = report.get("checks") or []
    if checks:
        step(2, 3, "自检")
        for c in checks:
            mark = _mark(c.get("level", "info"), c["ok"])
            name = c["name"].split(".", 1)[-1] if c["name"].startswith("smoke.") else c["name"]
            detail = c.get("detail", "")
            if not verbose and c.get("level") == "info":
                continue
            line(name, detail, mark, width=24)

    step(3, 3, "结论")
    if report.get("ok"):
        print("  安装完成。")
    else:
        print("  安装未完全成功，请按下列提示处理。")

    for w in report.get("warnings") or []:
        print(f"  ! {w}")
    for s in report.get("next_steps") or []:
        print(f"  → {s}")
    print()


def render_check_block(title: str, result: dict) -> None:
    header(title)
    for c in result.get("checks") or []:
        mark = _mark(c.get("level", "info"), c["ok"])
        line(c["name"], c.get("detail", ""), mark, width=24)
    print()
    print("  结果: " + ("通过" if result.get("ok") else "存在问题"))
    print()


def render_list(state: dict) -> None:
    header(f"locmodel-image {state.get('version')} · 安装状态")
    print(f"  安装目录: {state.get('install_dir')}  ({'已安装' if state.get('installed') else '未安装'})")
    print(f"  运行配置: {state.get('runtime_config')}")
    print()
    detected_label = {"present": "已安装", "creatable": "已安装(未配置)", "absent": "未检测到"}
    for h in state.get("hosts") or []:
        if h.get("path"):
            status = detected_label.get(h["detected"], h["detected"])
        else:
            status = "工具内注册"
        flag = "已注册" if h["registered"] else "未注册"
        print(f"  [{flag}] {h['label']}  —  {status}")
        if h.get("path"):
            print(f"          {h['path']}")
        if h["registered"]:
            print(f"          -> {h['command']} {' '.join(h.get('args') or [])}")
    print()
    print("  python 模式的解释器路径写死在宿主配置里，移动即失效；exe 模式无此问题。")
    print()


def render_uninstall(result: dict) -> None:
    header("locmodel-image · 卸载")
    for h in result.get("hosts") or []:
        status = h.get("status")
        label = h.get("label") or h.get("host")
        if status == "removed":
            ok(label, h.get("path", ""), width=24)
        elif status == "absent":
            info(label, "无注册项", width=24)
        elif status == "manual":
            info(label, "需在宿主内手动移除", width=24)
        else:
            fail(label, h.get("reason", str(status)), width=24)
    print()
    if result.get("purged"):
        print(f"  已删除程序目录: {result.get('install_dir')}")
    elif result.get("note"):
        print(f"  {result['note']}")
    if result.get("purge_error"):
        print(f"  ! 删除失败: {result['purge_error']}")
    print()


def pause_if_double_clicked(interactive: bool) -> None:
    if not interactive:
        return
    try:
        input("按回车键退出 ...")
    except (EOFError, KeyboardInterrupt):
        pass
