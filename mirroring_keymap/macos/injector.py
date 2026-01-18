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
        self._last_post_pos: Optional[Point] = None

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
        # 显式设置 delta，避免某些消费端（例如 iPhone Mirroring）在“抬起→重新按下”时
        # 依据系统内部 lastPos 计算出异常的大反向 delta。
        try:
            # Down/Up 本身不应该携带“跨位置的 delta”，否则可能被错误解释为拖动。
            if event_type in (
                Q.kCGEventLeftMouseDown,
                Q.kCGEventLeftMouseUp,
                Q.kCGEventRightMouseDown,
                Q.kCGEventRightMouseUp,
                getattr(Q, "kCGEventOtherMouseDown", -1),
                getattr(Q, "kCGEventOtherMouseUp", -1),
            ):
                dx = 0
                dy = 0
            else:
                if self._last_post_pos is None:
                    dx = 0
                    dy = 0
                else:
                    dx = int(round(float(pos[0]) - float(self._last_post_pos[0])))
                    dy = int(round(float(pos[1]) - float(self._last_post_pos[1])))
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventDeltaX, dx)
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventDeltaY, dy)
            # 尽量也填 unaccelerated（若常量存在）
            if hasattr(Q, "kCGMouseEventUnacceleratedDeltaX"):
                Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventUnacceleratedDeltaX, dx)
            if hasattr(Q, "kCGMouseEventUnacceleratedDeltaY"):
                Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventUnacceleratedDeltaY, dy)
        except Exception:
            pass
        Q.CGEventPost(Q.kCGHIDEventTap, ev)
        self._last_post_pos = (float(pos[0]), float(pos[1]))

    def get_cursor_pos(self) -> Point:
        Q = self._Quartz
        ev = Q.CGEventCreate(None)
        loc = Q.CGEventGetLocation(ev)
        return (float(loc.x), float(loc.y))

    def warp(self, pos: Point) -> None:
        self._Quartz.CGWarpMouseCursorPosition(pos)
        # 与 _post_mouse 保持一致，避免之后 delta 计算跳变过大
        self._last_post_pos = (float(pos[0]), float(pos[1]))

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
        # 不要在注入前 warp 光标：
        # - CGWarpMouseCursorPosition 可能触发系统生成的 mouseMoved 事件（无法打标），
        #   导致引擎把“自己的注入”误当成真实鼠标输入，从而抢占/打断摇杆与视角服务。
        # - 直接依赖 CGEventCreateMouseEvent 的位置参数即可。
        self._post_mouse(self._Quartz.kCGEventLeftMouseDown, pos, self._Quartz.kCGMouseButtonLeft)
        self._left_down = True

    def left_up(self, pos: Point) -> None:
        # 同 left_down：避免 warp 产生未打标事件。
        self._post_mouse(self._Quartz.kCGEventLeftMouseUp, pos, self._Quartz.kCGMouseButtonLeft)
        self._left_down = False

    def left_drag(self, pos: Point) -> None:
        # 注意：drag 事件隐含“左键按下”，因此要求外部先确保 left_down。
        # 同 left_down：避免 warp 产生未打标事件。
        self._post_mouse(self._Quartz.kCGEventLeftMouseDragged, pos, self._Quartz.kCGMouseButtonLeft)

    def move_cursor(self, pos: Point) -> None:
        """
        将系统光标移动到指定位置（通过 mouseMoved 事件，而不是 CGWarpMouseCursorPosition）。
        用途：在“视角触点到边界回中”时，先在抬起状态下把光标移回锚点，避免被 iPhone Mirroring
        误判为“按住状态下的反向拖动”。
        """
        self._post_mouse(self._Quartz.kCGEventMouseMoved, pos, self._Quartz.kCGMouseButtonLeft)

    def drag_smooth(self, start: Point, end: Point, *, max_step_px: float, step_delay_s: float = 0.0) -> None:
        if not self._left_down:
            self.left_down(start)
        cur = start
        for p in segment_points(start, end, max_step=max_step_px):
            cur = p
            self.left_drag(p)
            if step_delay_s > 0:
                time.sleep(step_delay_s)
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
