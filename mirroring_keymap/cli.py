from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

from .config import load_config, select_profile


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    profile = select_profile(cfg, args.profile)

    if args.dry_run and not args.run:
        # 只做配置解析与输出，不触发任何 macOS 捕获/注入。
        payload = {
            "version": cfg.version,
            "targetWindow": {
                "titleHint": cfg.targetWindow.titleHint,
                "pid": cfg.targetWindow.pid,
                "windowId": cfg.targetWindow.windowId,
            },
            "global": {
                "enableHotkey": cfg.global_.enableHotkey,
                "panicHotkey": cfg.global_.panicHotkey,
                "cameraLockKey": cfg.global_.cameraLockKey,
                "backpackKey": cfg.global_.backpackKey,
                "moveUpKey": cfg.global_.moveUpKey,
                "moveDownKey": cfg.global_.moveDownKey,
                "moveLeftKey": cfg.global_.moveLeftKey,
                "moveRightKey": cfg.global_.moveRightKey,
                "fireKey": cfg.global_.fireKey,
                "scopeKey": cfg.global_.scopeKey,
                "rrandDefaultPx": cfg.global_.rrandDefaultPx,
            },
            "profile": profile.name,
            "points": {k: list(v) for k, v in profile.points.items()},
            "customMappings": [
                {"name": m.name, "key": m.key, "type": m.type, "point": list(m.point)} for m in cfg.customMappings
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if sys.platform != "darwin":
        raise RuntimeError("--run 仅支持 macOS（Darwin）")

    from .engine import Engine
    from .macos.injector import Injector
    from .macos.input_capture import InputCapture

    log = logging.getLogger("mirroring_keymap")

    inj = Injector()
    engine = Engine(cfg, profile, inj, logger=log)
    capture = InputCapture(engine.handle_event, ignore_user_data_tag=inj.user_data_tag, logger=log)

    import Quartz

    loop = Quartz.CFRunLoopGetCurrent()

    def _shutdown(*_sig) -> None:
        log.info("shutting down...")
        try:
            capture.stop()
        except Exception:
            pass
        try:
            engine.stop()
        except Exception:
            pass
        try:
            Quartz.CFRunLoopStop(loop)
        except Exception:
            pass

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    engine.start()
    capture.start()
    log.info("running. (F8=enable/disable, F12=panic)")
    Quartz.CFRunLoopRun()
    _shutdown()
    return 0


def _cmd_pick(_args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        raise RuntimeError("pick 仅支持 macOS（Darwin）")

    import Quartz

    result: dict[str, float] = {}

    def _cb(proxy, event_type, event, refcon):
        if event_type == Quartz.kCGEventLeftMouseDown:
            loc = Quartz.CGEventGetLocation(event)
            result["x"] = float(loc.x)
            result["y"] = float(loc.y)
            Quartz.CFRunLoopStop(Quartz.CFRunLoopGetCurrent())
        return event

    mask = 1 << Quartz.kCGEventLeftMouseDown
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGHIDEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        mask,
        _cb,
        None,
    )
    if tap is None:
        raise RuntimeError("创建 EventTap 失败：请检查 Input Monitoring / Accessibility 权限。")

    src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    loop = Quartz.CFRunLoopGetCurrent()
    Quartz.CFRunLoopAddSource(loop, src, Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(tap, True)

    print("请在屏幕上左键点击一次以取点（按 Ctrl+C 取消）...")
    Quartz.CFRunLoopRun()

    if not result:
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mirroring-keymap")
    p.add_argument("--log-level", default="INFO", help="日志等级（DEBUG/INFO/WARN/ERROR）")
    p.add_argument("--config", default="config.json", help="配置文件路径（JSON）")
    p.add_argument("--profile", default=None, help="使用的配置档名称（默认第一个）")
    p.add_argument("--run", action="store_true", help="启用捕获/注入（危险：会吞输入）")
    p.add_argument("--dry-run", action="store_true", help="只解析配置并打印，不做捕获/注入")
    p.add_argument("cmd", nargs="?", default="run", choices=["run", "pick"], help="子命令（默认 run）")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)

    if args.cmd == "pick":
        return _cmd_pick(args)
    return _cmd_run(args)
