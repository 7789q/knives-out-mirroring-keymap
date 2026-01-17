from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque, Optional

from .config import AppConfig, CustomMapping, Point, ProfileConfig
from .mathutil import add, normalize, random_point, scale
from .macos.injector import CursorSnapshot, Injector
from .macos.keycodes import keycode_for
from .macos.window import is_target_frontmost


class Mode(str, Enum):
    PAUSED = "paused"
    BATTLE = "battle"
    FREE = "free"


@dataclass(frozen=True)
class TapRequest:
    name: str
    key_label: str
    point: Point
    hold_ms: int
    rrand_px: Optional[float]


@dataclass
class ClickMarker:
    x: float
    y: float
    label: str
    pressed_until_ts: float


@dataclass
class WheelSession:
    active: bool = False
    cursor_origin: Optional[Point] = None
    touch_origin: Optional[Point] = None
    touch_pos: Optional[Point] = None
    last_wheel_ts: float = 0.0
    pending_steps: int = 0  # 累积滚轮步数（正负）


class Engine:
    def __init__(
        self,
        cfg: AppConfig,
        profile: ProfileConfig,
        injector: Injector,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._cfg = cfg
        self._profile = profile
        self._inj = injector
        self._log = logger or logging.getLogger(__name__)

        self._rng = random.Random()

        # 热键预解析（失败时尽早报错）
        g = cfg.global_
        self._kc_enable = keycode_for(g.enableHotkey)
        self._kc_panic = keycode_for(g.panicHotkey)
        self._kc_caps = keycode_for(g.cameraLockKey)
        self._kc_backpack = keycode_for(g.backpackKey)

        # 移动方向键（默认 WASD，可配置）
        self._kc_move_up = keycode_for(g.moveUpKey)
        self._kc_move_down = keycode_for(g.moveDownKey)
        self._kc_move_left = keycode_for(g.moveLeftKey)
        self._kc_move_right = keycode_for(g.moveRightKey)

        # 开火/开镜触发键（可配置：MouseLeft/MouseRight/任意键）
        self._fire_trigger = self._parse_trigger(g.fireKey)
        self._scope_trigger = self._parse_trigger(g.scopeKey)

        # custom tap mappings
        self._custom_by_keycode: dict[int, CustomMapping] = {}
        for m in cfg.customMappings:
            try:
                self._custom_by_keycode[keycode_for(m.key)] = m
            except Exception as e:
                self._log.warning("忽略自定义映射 %r：%s", m.name, e)

        # 输入状态（由 EventTap 线程更新）
        self._lock = threading.Lock()
        self._keys_down: set[int] = set()
        self._mouse_dx_acc: float = 0.0
        self._mouse_dy_acc: float = 0.0
        self._tap_queue: Deque[TapRequest] = deque()
        self._wheel = WheelSession()

        # 运行状态（由调度线程更新）
        self._mapping_enabled: bool = False
        self._camera_lock: bool = False
        self._backpack_open: bool = False
        self._target_active: bool = False
        self._target_check_enabled: bool = bool(cfg.targetWindow.enabled)
        self._mode: Mode = Mode.PAUSED

        self._last_camera_ts: float = 0.0
        self._last_joystick_ts: float = 0.0
        self._target_check_ts: float = 0.0

        self._battle_cursor_snap: Optional[CursorSnapshot] = None

        # 供 UI 覆盖层显示“按键点击点位”（蓝/橙）
        self._click_markers: dict[str, ClickMarker] = {}

        # 权限检测（辅助功能/注入）
        self._accessibility_trusted: Optional[bool] = None

        # 日志：避免在 tick 循环里刷屏，仅在变化时输出
        self._last_target_active_logged: Optional[bool] = None
        self._ignore_log_last: dict[str, float] = {}

        self._stop_evt = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name="scheduler", daemon=True)

    def _parse_trigger(self, spec: str) -> tuple[str, object]:
        s = (spec or "").strip().lower()
        if s in ("mouseleft", "leftmouse", "mouse1", "lmb", "left"):
            return ("mouse", "left")
        if s in ("mouseright", "rightmouse", "mouse2", "rmb", "right"):
            return ("mouse", "right")
        # 默认按键名（例如 "J" / "Space" / "UpArrow"）
        return ("key", keycode_for(spec))

    # -------------------------
    # Public API
    # -------------------------

    def start(self) -> None:
        self._log.info("Engine starting...")
        self._check_permissions()
        if not self._target_check_enabled:
            self._log.info("已关闭目标窗口检测：映射启用时将对所有前台应用生效")
        self._thread.start()

    def stop(self) -> None:
        self._log.info("Engine stopping...")
        self._stop_evt.set()
        self._thread.join(timeout=1.0)
        self._safe_release_all()
        self._log.info("Engine stopped.")

    def is_mapping_enabled(self) -> bool:
        with self._lock:
            return self._mapping_enabled

    def current_mode(self) -> Mode:
        with self._lock:
            return self._mode

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "mapping_enabled": self._mapping_enabled,
                "camera_lock": self._camera_lock,
                "backpack_open": self._backpack_open,
                "target_check_enabled": self._target_check_enabled,
                "target_active": self._target_active,
                "mode": self._mode.value,
                "accessibility_trusted": self._accessibility_trusted,
            }

    def click_markers(self) -> list[dict[str, object]]:
        now = time.monotonic()
        with self._lock:
            return [
                {
                    "x": m.x,
                    "y": m.y,
                    "label": m.label,
                    "pressed": now < float(m.pressed_until_ts),
                }
                for m in self._click_markers.values()
            ]

    def _check_permissions(self) -> None:
        # 仅用于日志/提示，不尝试触发系统弹窗，避免影响体验。
        try:
            import Quartz

            self._accessibility_trusted = bool(Quartz.AXIsProcessTrusted())
        except Exception:
            self._accessibility_trusted = None

        if self._accessibility_trusted is False:
            self._log.warning("未授予“辅助功能”权限：可能无法注入点击/拖动。请到 系统设置 → 隐私与安全性 → 辅助功能 授权。")

    def set_mapping_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._mapping_enabled = bool(enabled)
            if not self._mapping_enabled:
                self._camera_lock = False
                self._backpack_open = False
        self._log.info("映射 %s", "启用" if enabled else "禁用")
        if not enabled:
            self._safe_release_all()

    def panic(self) -> None:
        with self._lock:
            self._mapping_enabled = False
            self._camera_lock = False
            self._backpack_open = False
        self._log.warning("紧急停止：已禁用映射并请求释放所有按住")
        self._safe_release_all()

    def set_camera_lock(self, enabled: bool) -> None:
        with self._lock:
            self._camera_lock = bool(enabled)
            if self._camera_lock:
                self._backpack_open = False
        self._log.info("视角锁定 %s", "开启" if enabled else "关闭")

    def toggle_backpack(self) -> None:
        with self._lock:
            opening = not self._backpack_open
            self._backpack_open = opening
            self._camera_lock = False if opening else True
            self._tap_queue.append(
                TapRequest(
                    name="backpack",
                    key_label=self._cfg.global_.backpackKey,
                    point=self._profile.points["backpack"],
                    hold_ms=self._profile.fire.tapHoldMs,
                    rrand_px=self._cfg.global_.rrandDefaultPx,
                )
            )
        self._log.info("背包 %s", "打开" if opening else "关闭")

    # -------------------------
    # EventTap callback
    # -------------------------

    def handle_event(self, event_type: int, event) -> bool:
        """
        返回 True 表示吞掉事件（不传递给系统）。
        注意：此函数在 EventTap 回调里被调用，必须尽量轻量。
        """
        import Quartz

        now = time.monotonic()

        swallow = False
        with self._lock:
            mapping_enabled = self._mapping_enabled
            target_active = self._target_active
            mode = self._mode

        # 即使映射未启用，也允许热键生效（但不吞输入）。
        if event_type in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp, Quartz.kCGEventFlagsChanged):
            kc = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))

            is_down = event_type != Quartz.kCGEventKeyUp
            with self._lock:
                if is_down:
                    self._keys_down.add(kc)
                else:
                    self._keys_down.discard(kc)

            if event_type == Quartz.kCGEventKeyDown:
                if kc == self._kc_enable:
                    with self._lock:
                        self._mapping_enabled = not self._mapping_enabled
                        if not self._mapping_enabled:
                            self._camera_lock = False
                            self._backpack_open = False
                    self._log.info("映射 %s", "启用" if self.is_mapping_enabled() else "禁用")
                elif kc == self._kc_panic:
                    with self._lock:
                        self._mapping_enabled = False
                        self._camera_lock = False
                        self._backpack_open = False
                    self._log.warning("紧急停止：已禁用映射并请求释放所有按住")
                    self._safe_release_all()
                elif kc == self._kc_backpack and mapping_enabled and target_active:
                    # 背包切换：开 -> 强制自由鼠标；关 -> 自动回战斗且开启视角锁定
                    with self._lock:
                        opening = not self._backpack_open
                        self._backpack_open = opening
                        self._camera_lock = False if opening else True
                        self._tap_queue.append(
                            TapRequest(
                                name="backpack",
                                key_label=self._cfg.global_.backpackKey,
                                point=self._profile.points["backpack"],
                                hold_ms=self._profile.fire.tapHoldMs,
                                rrand_px=self._cfg.global_.rrandDefaultPx,
                            )
                        )
                    self._log.info("背包 %s", "打开" if opening else "关闭")
                else:
                    # 内置开火/开镜：若触发键是键盘，则在此处拦截并转为 Tap
                    t_type, t_val = self._fire_trigger
                    if t_type == "key" and kc == int(t_val):
                        if not mapping_enabled:
                            self._log.warning("开火被忽略：映射未启用")
                        elif not target_active:
                            self._log.warning("开火被忽略：目标窗口未在前台（请检查 titleHint/pid）")
                        elif mode != Mode.BATTLE:
                            self._log.warning("开火被忽略：当前模式=%s（需要战斗模式）", mode.value)
                        else:
                            with self._lock:
                                self._tap_queue.append(
                                    TapRequest(
                                        name="fire",
                                        key_label=self._cfg.global_.fireKey,
                                        point=self._profile.points["fire"],
                                        hold_ms=self._profile.fire.tapHoldMs,
                                        rrand_px=self._profile.fire.rrandPx,
                                    )
                                )
                            self._log.debug("enqueue tap: fire")
                            swallow = True
                            return True

                    t_type, t_val = self._scope_trigger
                    if t_type == "key" and kc == int(t_val):
                        if not mapping_enabled:
                            self._log.warning("开镜被忽略：映射未启用")
                        elif not target_active:
                            self._log.warning("开镜被忽略：目标窗口未在前台（请检查 titleHint/pid）")
                        elif mode != Mode.BATTLE:
                            self._log.warning("开镜被忽略：当前模式=%s（需要战斗模式）", mode.value)
                        else:
                            with self._lock:
                                self._tap_queue.append(
                                    TapRequest(
                                        name="scope",
                                        key_label=self._cfg.global_.scopeKey,
                                        point=self._profile.points["scope"],
                                        hold_ms=self._profile.scope.tapHoldMs,
                                        rrand_px=self._profile.scope.rrandPx,
                                    )
                                )
                            self._log.debug("enqueue tap: scope")
                            swallow = True
                            return True

                    # 自定义映射（按键→点击）
                    if event_type == Quartz.kCGEventKeyDown and kc in self._custom_by_keycode:
                        m = self._custom_by_keycode[kc]
                        if not mapping_enabled:
                            self._log.warning("自定义点击「%s」被忽略：映射未启用", m.name)
                        elif not target_active:
                            self._log.warning("自定义点击「%s」被忽略：目标窗口未在前台（请检查 titleHint/pid）", m.name)
                        elif mode != Mode.BATTLE:
                            self._log.warning("自定义点击「%s」被忽略：当前模式=%s（需要战斗模式）", m.name, mode.value)
                        else:
                            with self._lock:
                                self._tap_queue.append(
                                    TapRequest(
                                        name=f"custom:{m.key}",
                                        key_label=f"{m.key}:{m.name}" if m.name else m.key,
                                        point=m.point,
                                        hold_ms=m.tapHoldMs,
                                        rrand_px=m.rrandPx,
                                    )
                                )
                            self._log.debug("enqueue tap: custom:%s", m.key)

            # CapsLock 使用 flagsChanged 更可靠
            if event_type == Quartz.kCGEventFlagsChanged and kc == self._kc_caps and mapping_enabled and target_active:
                with self._lock:
                    self._camera_lock = not self._camera_lock
                    self._log.info("视角锁定 %s", "开启" if self._camera_lock else "关闭")
                    if self._camera_lock:
                        self._backpack_open = False
                self._log.info("视角锁定 %s", "开启" if self._camera_lock else "关闭")

            # 吞吐策略
            if mapping_enabled and target_active and mode == Mode.BATTLE:
                # 战斗态吞掉键盘输入，避免系统快捷键/其他应用行为
                swallow = True

        elif event_type == Quartz.kCGEventMouseMoved:
            if mapping_enabled and target_active and mode == Mode.BATTLE:
                dx = float(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaX))
                dy = float(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaY))
                with self._lock:
                    self._mouse_dx_acc += dx
                    self._mouse_dy_acc += dy
                swallow = True

        elif event_type in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventRightMouseDown):
            def _throttle(key: str, interval_s: float = 0.8) -> bool:
                last = self._ignore_log_last.get(key, 0.0)
                if now - last < interval_s:
                    return False
                self._ignore_log_last[key] = now
                return True

            # 鼠标触发下：当用户已启用映射但条件不满足时，给出更明确的日志（避免“按了没反应”）
            if (self._fire_trigger[0] == "mouse" or self._scope_trigger[0] == "mouse") and mapping_enabled:
                if not target_active and _throttle("mouse_click_ignored_target"):
                    self._log.warning("鼠标点击映射被忽略：目标窗口未在前台/未命中（可在配置中关闭目标检测）")
                elif mode != Mode.BATTLE and _throttle("mouse_click_ignored_mode"):
                    self._log.warning("鼠标点击映射被忽略：当前模式=%s（需要视角锁定/战斗模式）", mode.value)

            if mapping_enabled and target_active and mode == Mode.BATTLE:
                # 用 Tap 代替原生点击（仅当触发键为鼠标时）
                t_type, t_val = self._fire_trigger
                if t_type == "mouse":
                    if t_val == "left" and event_type == Quartz.kCGEventLeftMouseDown:
                        with self._lock:
                            self._tap_queue.append(
                                TapRequest(
                                    name="fire",
                                    key_label=self._cfg.global_.fireKey,
                                    point=self._profile.points["fire"],
                                    hold_ms=self._profile.fire.tapHoldMs,
                                    rrand_px=self._profile.fire.rrandPx,
                                )
                            )
                        self._log.debug("enqueue tap: fire (mouse)")
                        swallow = True
                    if t_val == "right" and event_type == Quartz.kCGEventRightMouseDown:
                        with self._lock:
                            self._tap_queue.append(
                                TapRequest(
                                    name="fire",
                                    key_label=self._cfg.global_.fireKey,
                                    point=self._profile.points["fire"],
                                    hold_ms=self._profile.fire.tapHoldMs,
                                    rrand_px=self._profile.fire.rrandPx,
                                )
                            )
                        self._log.debug("enqueue tap: fire (mouse)")
                        swallow = True

                t_type, t_val = self._scope_trigger
                if t_type == "mouse":
                    if t_val == "left" and event_type == Quartz.kCGEventLeftMouseDown:
                        with self._lock:
                            self._tap_queue.append(
                                TapRequest(
                                    name="scope",
                                    key_label=self._cfg.global_.scopeKey,
                                    point=self._profile.points["scope"],
                                    hold_ms=self._profile.scope.tapHoldMs,
                                    rrand_px=self._profile.scope.rrandPx,
                                )
                            )
                        self._log.debug("enqueue tap: scope (mouse)")
                        swallow = True
                    if t_val == "right" and event_type == Quartz.kCGEventRightMouseDown:
                        with self._lock:
                            self._tap_queue.append(
                                TapRequest(
                                    name="scope",
                                    key_label=self._cfg.global_.scopeKey,
                                    point=self._profile.points["scope"],
                                    hold_ms=self._profile.scope.tapHoldMs,
                                    rrand_px=self._profile.scope.rrandPx,
                                )
                            )
                        self._log.debug("enqueue tap: scope (mouse)")
                        swallow = True

        elif event_type == Quartz.kCGEventScrollWheel:
            # 滚轮映射：
            # - 锁定视角（战斗模式）：使用配置的 anchorPoint 上下拖动
            # - 解锁视角（自由鼠标）：以鼠标当前位置上下拖动（并在结束后回位）
            # 吞掉滚轮，避免双触发
            if mapping_enabled and target_active and self._profile.wheel.enabled and mode in (Mode.FREE, Mode.BATTLE):
                delta = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGScrollWheelEventDeltaAxis1))
                if self._profile.wheel.invert:
                    delta = -delta
                if delta != 0:
                    if mode == Mode.BATTLE:
                        anchor = self._profile.wheel.anchorPoint or self._profile.points["cameraAnchor"]
                        touch_origin = anchor
                        cursor_origin = None  # 战斗模式不做回位（退出战斗时会 restore_cursor）
                    else:
                        loc = Quartz.CGEventGetLocation(event)
                        pos = (float(loc.x), float(loc.y))
                        touch_origin = pos
                        cursor_origin = pos
                    with self._lock:
                        if not self._wheel.active:
                            self._wheel.active = True
                            self._wheel.cursor_origin = cursor_origin
                            self._wheel.touch_origin = touch_origin
                            self._wheel.touch_pos = None  # 首次 tick 决定随机落点并 down
                        self._wheel.last_wheel_ts = now
                        self._wheel.pending_steps += 1 if delta > 0 else -1
                swallow = True

        _ = now
        return swallow

    # -------------------------
    # Scheduler loop
    # -------------------------

    def _run_loop(self) -> None:
        tick_hz = max(10, int(self._profile.scheduler.tickHz))
        tick_dt = 1.0 / float(tick_hz)

        while not self._stop_evt.is_set():
            t0 = time.monotonic()
            try:
                self._tick(t0)
            except Exception as e:
                self._log.exception("tick error: %s", e)

            elapsed = time.monotonic() - t0
            sleep_s = max(0.0, tick_dt - elapsed)
            time.sleep(sleep_s)

    def _tick(self, now: float) -> None:
        # 目标窗口检查不需要每 tick 都做
        if now - self._target_check_ts >= 0.2:
            if not self._target_check_enabled:
                active = True
            else:
                active = False
                try:
                    active = is_target_frontmost(self._cfg.targetWindow)
                except Exception as e:
                    self._log.debug("target check failed: %s", e)
            with self._lock:
                self._target_active = active
                self._target_check_ts = now
            if self._target_check_enabled:
                if self._last_target_active_logged is None or self._last_target_active_logged != active:
                    self._last_target_active_logged = active
                    self._log.info("目标窗口在前台：%s", "是" if active else "否")

        with self._lock:
            mapping_enabled = self._mapping_enabled
            target_active = self._target_active
            camera_lock = self._camera_lock
            backpack_open = self._backpack_open

        if not mapping_enabled or not target_active:
            self._set_mode(Mode.PAUSED)
            self._safe_release_all()
            return

        if backpack_open or not camera_lock:
            self._set_mode(Mode.FREE)
            self._tick_free(now)
            return

        self._set_mode(Mode.BATTLE)
        self._tick_battle(now)

    def _set_mode(self, mode: Mode) -> None:
        with self._lock:
            if self._mode == mode:
                return
            prev = self._mode
            self._mode = mode

        # 处理模式切换的副作用（不要在锁内做注入）
        if prev == Mode.BATTLE and mode != Mode.BATTLE:
            # 退出战斗态：释放并恢复光标
            self._safe_release_all()
            if self._battle_cursor_snap is not None:
                try:
                    self._inj.restore_cursor(self._battle_cursor_snap)
                except Exception:
                    pass
                self._battle_cursor_snap = None

        if mode == Mode.BATTLE and prev != Mode.BATTLE:
            # 进入战斗态：记录光标位置并隐藏
            try:
                self._battle_cursor_snap = self._inj.snapshot_cursor()
                self._inj.hide_cursor()
            except Exception:
                self._battle_cursor_snap = None

        self._log.info("mode: %s -> %s", prev.value, mode.value)

    def _tick_free(self, now: float) -> None:
        # 自由鼠标态：不做任何坐标映射，除滚轮映射外。
        if self._service_wheel(now):
            return
        self._safe_release_all()

    def _service_wheel(self, now: float) -> bool:
        with self._lock:
            active = self._wheel.active
            pending_steps = self._wheel.pending_steps
            last_ts = self._wheel.last_wheel_ts
            cursor_origin = self._wheel.cursor_origin
            touch_origin = self._wheel.touch_origin
            touch_pos = self._wheel.touch_pos

        if not active:
            return False

        # 首次 tick：确定落点并按下
        if touch_pos is None:
            origin = touch_origin or cursor_origin or self._inj.get_cursor_pos()
            r = self._profile.wheel.rrandPx
            if r is None:
                r = self._cfg.global_.rrandDefaultPx
            p0 = random_point(origin, float(r or 0.0), rng=self._rng)
            try:
                self._inj.left_down(p0)
            except Exception as e:
                self._log.debug("wheel down failed: %s", e)
                with self._lock:
                    self._wheel = WheelSession()  # reset
                # 避免战斗态光标意外显示
                try:
                    if self.current_mode() == Mode.BATTLE:
                        self._inj.hide_cursor()
                except Exception:
                    pass
                return True
            with self._lock:
                # 若期间被重置，则不强行覆盖
                if self._wheel.active and self._wheel.touch_pos is None:
                    self._wheel.touch_pos = p0
                    touch_pos = p0

        # 判断停止
        stop_s = max(0.01, self._profile.wheel.stopMs / 1000.0)
        if now - last_ts > stop_s:
            try:
                up_pos = touch_pos or self._inj.get_cursor_pos()
                self._inj.left_up(up_pos)
            except Exception:
                pass
            # 自由鼠标模式：滚动结束后回位；战斗模式：不回位（退出战斗时 restore_cursor）
            if cursor_origin is not None:
                try:
                    self._inj.warp(cursor_origin)
                except Exception:
                    pass
            with self._lock:
                self._wheel = WheelSession()
            return True

        if pending_steps == 0:
            return True

        # 消耗一个 step
        step = 1 if pending_steps > 0 else -1
        with self._lock:
            self._wheel.pending_steps -= step

        cur = touch_pos
        if cur is None:
            return True
        target = (cur[0], cur[1] + step * float(self._profile.wheel.dPx))
        try:
            self._inj.drag_smooth(cur, target, max_step_px=self._profile.scheduler.maxStepPx)
            with self._lock:
                if self._wheel.active:
                    self._wheel.touch_pos = target
        except Exception:
            # 出错则重置，避免卡住；同时尽量恢复战斗态光标隐藏状态
            try:
                self._inj.release_all()
            except Exception:
                pass
            try:
                if self.current_mode() == Mode.BATTLE:
                    self._inj.hide_cursor()
            except Exception:
                pass
            with self._lock:
                self._wheel = WheelSession()
        return True

    def _tick_battle(self, now: float) -> None:
        # 1) Tap 抢占
        req: Optional[TapRequest] = None
        with self._lock:
            if self._tap_queue:
                req = self._tap_queue.popleft()
        if req is not None:
            self._service_tap(req)
            return

        # 1.5) Wheel（滚轮映射会占用左键拖动，因此在战斗态优先独占执行）
        if self._service_wheel(now):
            return

        # 2) Camera / Joystick 轮转
        mouse_dx, mouse_dy = 0.0, 0.0
        keys_down: set[int] = set()
        with self._lock:
            mouse_dx = self._mouse_dx_acc
            mouse_dy = self._mouse_dy_acc
            keys_down = set(self._keys_down)

        want_move = any(
            k in keys_down for k in (self._kc_move_up, self._kc_move_down, self._kc_move_left, self._kc_move_right)
        )

        camera_interval = 1.0 / max(1.0, float(self._profile.scheduler.cameraMinHz))
        joy_interval = 1.0 / max(1.0, float(self._profile.scheduler.joystickMinHz))

        camera_due = (now - self._last_camera_ts) >= camera_interval and (abs(mouse_dx) + abs(mouse_dy)) > 0.0
        joy_due = (now - self._last_joystick_ts) >= joy_interval and want_move

        # 若两者都 due，优先服务更紧急的（按 overdue 比例）
        if camera_due and joy_due:
            cam_over = (now - self._last_camera_ts) / camera_interval
            joy_over = (now - self._last_joystick_ts) / joy_interval
            if cam_over >= joy_over:
                self._service_camera(mouse_dx, mouse_dy)
            else:
                self._service_joystick(keys_down)
            return

        if camera_due:
            self._service_camera(mouse_dx, mouse_dy)
            return

        if joy_due:
            self._service_joystick(keys_down)
            return

        # 不 due 时：如果有鼠标输入，依然尽快服务 camera（更贴手）
        tcam = float(self._profile.camera.tcamPx)
        if abs(mouse_dx) + abs(mouse_dy) >= tcam:
            self._service_camera(mouse_dx, mouse_dy)
            return

        # 否则如果在移动，则服务 joystick
        if want_move:
            self._service_joystick(keys_down)

    def _service_tap(self, req: TapRequest) -> None:
        # Tap 前先抬起，避免与 drag 状态混淆
        self._safe_release_all()
        p = req.point
        r = req.rrand_px
        if r is None:
            r = self._cfg.global_.rrandDefaultPx
        p2 = random_point(p, float(r or 0.0), rng=self._rng)
        label = (req.key_label or req.name).strip()

        # 记录本次“实际点击点位”，供 UI 覆盖层显示：默认蓝色，按下时橙色。
        with self._lock:
            # Tap 按压一般很短（默认 30ms），UI 刷新频率较低时会看不到“橙色按下态”。
            # 因此这里强制至少保留一小段“按下”窗口，便于用户观察与排查。
            hold_s = max(0.0, float(req.hold_ms) / 1000.0)
            pressed_until = time.monotonic() + max(0.25, hold_s)
            self._click_markers[req.name] = ClickMarker(x=p2[0], y=p2[1], label=label, pressed_until_ts=pressed_until)

        self._log.info("点击: %s (键=%s) @ (%.1f, %.1f)", req.name, label, p2[0], p2[1])
        try:
            self._inj.tap(p2, hold_ms=req.hold_ms)
        finally:
            # Tap 后恢复到“战斗态默认”——如果当前仍是战斗态，则继续隐藏光标
            try:
                if self.current_mode() == Mode.BATTLE:
                    self._inj.hide_cursor()
            except Exception:
                pass

    def _service_camera(self, dx: float, dy: float) -> None:
        # 以 A 为锚点做一次短拖动：down -> drag -> up
        with self._lock:
            self._mouse_dx_acc = 0.0
            self._mouse_dy_acc = 0.0

        cam = self._profile.camera
        a = self._profile.points["cameraAnchor"]

        sx = dx * float(cam.sensitivity)
        sy = dy * float(cam.sensitivity) * (-1.0 if cam.invertY else 1.0)

        # 限幅
        r = float(cam.radiusPx)
        sx = max(-r, min(r, sx))
        sy = max(-r, min(r, sy))
        end = (a[0] + sx, a[1] + sy)

        try:
            self._inj.left_down(a)
            self._inj.drag_smooth(a, end, max_step_px=self._profile.scheduler.maxStepPx)
            self._inj.left_up(end)
            self._last_camera_ts = time.monotonic()
        except Exception:
            self._safe_release_all()

    def _service_joystick(self, keys_down: set[int]) -> None:
        c = self._profile.points["joystickCenter"]
        joy = self._profile.joystick

        vx = 0.0
        vy = 0.0
        if self._kc_move_up in keys_down:
            vy += 1.0
        if self._kc_move_down in keys_down:
            vy -= 1.0
        if self._kc_move_left in keys_down:
            vx -= 1.0
        if self._kc_move_right in keys_down:
            vx += 1.0
        v = normalize((vx, vy))
        target = add(c, scale(v, float(joy.radiusPx)))

        try:
            self._inj.left_down(c)
            self._inj.drag_smooth(c, target, max_step_px=self._profile.scheduler.maxStepPx)
            # 轻微停留，减少“点一下就松”导致的不稳
            time.sleep(0.006)
            self._inj.left_up(target)
            self._last_joystick_ts = time.monotonic()
        except Exception:
            self._safe_release_all()

    def _safe_release_all(self) -> None:
        try:
            self._inj.release_all()
        except Exception:
            pass
