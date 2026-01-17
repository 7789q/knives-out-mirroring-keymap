from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from . import require_macos


EventHandler = Callable[[int, object], bool]


@dataclass
class TapHandle:
    tap: object
    run_loop_source: object


class InputCapture:
    def __init__(
        self,
        handler: EventHandler,
        *,
        ignore_user_data_tag: Optional[int] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        require_macos()
        self._handler = handler
        self._ignore_tag = ignore_user_data_tag
        self._log = logger or logging.getLogger(__name__)
        self._handle: Optional[TapHandle] = None
        self._active: bool = False

    def start(self) -> None:
        require_macos()
        import Quartz

        if self._handle is not None:
            return

        mask = 0
        for et in (
            Quartz.kCGEventKeyDown,
            Quartz.kCGEventKeyUp,
            Quartz.kCGEventFlagsChanged,
            Quartz.kCGEventMouseMoved,
            Quartz.kCGEventLeftMouseDown,
            Quartz.kCGEventLeftMouseUp,
            Quartz.kCGEventLeftMouseDragged,
            Quartz.kCGEventRightMouseDown,
            Quartz.kCGEventRightMouseUp,
            Quartz.kCGEventScrollWheel,
        ):
            mask |= 1 << et

        def _cb(proxy, event_type, event, refcon):
            # 处理 tap 被系统禁用的情况
            if event_type in (
                Quartz.kCGEventTapDisabledByTimeout,
                Quartz.kCGEventTapDisabledByUserInput,
            ):
                # stop() 后仍可能收到一次 disable 回调；此时不要误报/重启。
                if not self._active or self._handle is None:
                    return event
                self._log.warning("EventTap disabled (%s). Re-enabling...", event_type)
                try:
                    Quartz.CGEventTapEnable(self._handle.tap, True)  # type: ignore[union-attr]
                except Exception:
                    pass
                return event

            # 过滤本工具注入事件的回流
            if self._ignore_tag is not None:
                try:
                    tag = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData)
                    if int(tag) == int(self._ignore_tag):
                        return event
                except Exception:
                    pass

            try:
                swallow = bool(self._handler(int(event_type), event))
                return None if swallow else event
            except Exception as e:
                self._log.exception("EventTap callback error: %s", e)
                return event

        tap = Quartz.CGEventTapCreate(
            Quartz.kCGHIDEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            mask,
            _cb,
            None,
        )
        if tap is None:
            raise RuntimeError(
                "创建 EventTap 失败：请检查 Input Monitoring / Accessibility 权限是否已授予，并重启相关进程。"
            )

        src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(loop, src, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)

        self._handle = TapHandle(tap=tap, run_loop_source=src)
        self._active = True
        self._log.info("InputCapture started.")

    def stop(self) -> None:
        require_macos()
        import Quartz

        if self._handle is None:
            return
        self._active = False
        try:
            Quartz.CGEventTapEnable(self._handle.tap, False)
        except Exception:
            pass
        self._handle = None
        self._log.info("InputCapture stopped.")
