from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from ..config import Point
from ..mathutil import segment_points
from . import require_macos


@dataclass
class CursorSnapshot:
    pos: Point
    hidden: bool


class Injector:
    def __init__(self, *, user_data_tag: int = 0x4B4D4D50) -> None:
        require_macos()
        import Quartz

        self._Quartz = Quartz
        self._display = Quartz.CGMainDisplayID()
        self._user_data_tag = int(user_data_tag)
        self._cursor_hidden = False
        self._left_down = False

    def _mark_event(self, event) -> None:
        # 尝试在事件上打标，供 InputCapture 过滤回流。
        Q = self._Quartz
        try:
            Q.CGEventSetIntegerValueField(event, Q.kCGEventSourceUserData, self._user_data_tag)
        except Exception:
            pass

    def _post_mouse(self, event_type: int, pos: Point, button: int) -> None:
        Q = self._Quartz
        ev = Q.CGEventCreateMouseEvent(None, event_type, pos, button)
        self._mark_event(ev)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)

    def get_cursor_pos(self) -> Point:
        Q = self._Quartz
        ev = Q.CGEventCreate(None)
        loc = Q.CGEventGetLocation(ev)
        return (float(loc.x), float(loc.y))

    def warp(self, pos: Point) -> None:
        self._Quartz.CGWarpMouseCursorPosition(pos)

    def hide_cursor(self) -> None:
        if self._cursor_hidden:
            return
        self._Quartz.CGDisplayHideCursor(self._display)
        self._cursor_hidden = True

    def show_cursor(self) -> None:
        if not self._cursor_hidden:
            return
        self._Quartz.CGDisplayShowCursor(self._display)
        self._cursor_hidden = False

    def snapshot_cursor(self) -> CursorSnapshot:
        return CursorSnapshot(pos=self.get_cursor_pos(), hidden=self._cursor_hidden)

    def restore_cursor(self, snap: CursorSnapshot) -> None:
        if snap.hidden:
            self.hide_cursor()
        else:
            self.show_cursor()
        self.warp(snap.pos)

    def left_down(self, pos: Point) -> None:
        self.warp(pos)
        self._post_mouse(self._Quartz.kCGEventLeftMouseDown, pos, self._Quartz.kCGMouseButtonLeft)
        self._left_down = True

    def left_up(self, pos: Point) -> None:
        self.warp(pos)
        self._post_mouse(self._Quartz.kCGEventLeftMouseUp, pos, self._Quartz.kCGMouseButtonLeft)
        self._left_down = False

    def left_drag(self, pos: Point) -> None:
        # 注意：drag 事件隐含“左键按下”，因此要求外部先确保 left_down。
        self.warp(pos)
        self._post_mouse(self._Quartz.kCGEventLeftMouseDragged, pos, self._Quartz.kCGMouseButtonLeft)

    def drag_smooth(self, start: Point, end: Point, *, max_step_px: float) -> None:
        if not self._left_down:
            self.left_down(start)
        cur = start
        for p in segment_points(start, end, max_step=max_step_px):
            cur = p
            self.left_drag(p)
        # 保持按住，由调用方决定何时 up
        _ = cur

    def tap(self, pos: Point, *, hold_ms: int = 30) -> None:
        self.left_down(pos)
        time.sleep(max(0.0, hold_ms / 1000.0))
        self.left_up(pos)

    def release_all(self) -> None:
        # MVP 仅处理左键。幂等调用。
        if self._left_down:
            try:
                self.left_up(self.get_cursor_pos())
            except Exception:
                # 即使 up 失败，也要尽量恢复状态
                self._left_down = False
        self.show_cursor()

    @property
    def user_data_tag(self) -> int:
        return self._user_data_tag

