from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque, Optional


def _secure_input_enabled() -> Optional[bool]:
    """
    macOS “安全输入”(Secure Event Input) 开启时，系统会阻止第三方进程读取键盘事件。
    这会导致 EventTap/轮询都读不到 WASD，从而表现为“摇杆完全无反应”。
    """
    try:
        import ctypes
        from ctypes import c_bool

        lib = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        fn = lib.CGSIsSecureEventInputSet
        fn.restype = c_bool
        return bool(fn())
    except Exception:
        return None

from .config import AppConfig, CustomMapping, Point, ProfileConfig
from .mathutil import add, normalize, random_point, scale
from .macos.injector import CursorSnapshot, Injector
from .macos.keycodes import keycode_for
from .macos.window import get_frontmost, is_target_frontmost


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
    # 背包/菜单类按钮通常需要“抬起后再按一次”才可靠（避免触点被其他 session 占用）
    pre_release: bool = True
    # 有些 UI 按钮（例如背包）在刚切换模式/刚释放触点后需要稍微等待一下才稳定命中
    pre_delay_ms: int = 0


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


@dataclass
class JoystickSession:
    active: bool = False
    center_pos: Optional[Point] = None
    touch_pos: Optional[Point] = None
    last_ts: float = 0.0


@dataclass
class CameraSession:
    """
    视角触点会“按住并小范围拖动”，避免每帧 down->up 导致的突兀/不生效。
    注意：本工具受“单指限制”，Camera 与 Joystick/Wheel 需要时间片切换。
    """

    active: bool = False
    touch_pos: Optional[Point] = None
    last_drag_ts: float = 0.0


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
        self._camera_lock_key_name = str(g.cameraLockKey or "CapsLock").strip() or "CapsLock"
        self._kc_caps = keycode_for(self._camera_lock_key_name)
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
        # 最近一次“真实鼠标移动事件”的时间戳（仅用于诊断/状态机）
        self._last_mouse_event_ts: float = 0.0
        # 最近一次“真实鼠标移动事件”的光标位置（Quartz 全局坐标）
        self._last_mouse_loc: Optional[Point] = None
        self._tap_queue: Deque[TapRequest] = deque()
        self._wheel = WheelSession()
        self._joy_session = JoystickSession()
        self._cam_session = CameraSession()
        # 视角回中状态：到达边界后先抬起，等待下一段“新的鼠标轨迹”再重新按下，避免回中反向拖动。
        self._cam_recenter_pending: bool = False
        self._cam_recenter_pos: Optional[Point] = None
        self._cam_recenter_mouse_ts0: float = 0.0
        # 回中流程分两步：先 release + warp 回锚点（会触发未打标 mouseMoved），再等待用户真实移动开始下一段。
        # ready=False 时 _maybe_start_camera_after_recenter 不会启动（避免竞态：用户移动事件先到、warp 还未执行）。
        self._cam_recenter_ready: bool = False

        # 键盘状态兜底：部分场景（例如系统安全输入）可能导致 EventTap 收不到键盘事件。
        # 这里通过轮询 CGEventSourceKeyState 做“WASD/热键/自定义按键”的备用输入源。
        self._polled_keys_down: set[int] = set()
        # 仅用于“移动键轮询是否可靠”的判断：只有当轮询曾经检测到过移动键按下，
        # 才用轮询结果去覆盖 EventTap（否则在轮询失效时会导致 WASD 永远无反应）。
        self._poll_move_ok: bool = False
        # 诊断：最近一次捕获到任意键盘事件的时间（monotonic）；用于排查“只收得到鼠标，不收键盘”。
        self._last_kbd_event_ts: float = 0.0
        self._poll_prev_down: dict[int, bool] = {}
        self._action_last_ts: dict[str, float] = {}
        self._action_lock = threading.Lock()

        # 运行状态（由调度线程更新）
        self._mapping_enabled: bool = False
        self._camera_lock: bool = False
        self._backpack_open: bool = False
        self._target_active: bool = False
        # 用户诉求：不需要检测目标窗口。映射启用时默认对所有前台应用生效（但前台是本程序时会自动暂停）。
        self._target_check_enabled: bool = False
        self._mode: Mode = Mode.PAUSED

        self._last_camera_ts: float = 0.0
        self._last_joystick_ts: float = 0.0
        # 为了让摇杆在游戏端更稳定生效，需要在“切到视角/开火”前至少保持一小段按住窗口。
        self._joy_hold_until_ts: float = 0.0
        self._target_check_ts: float = 0.0

        self._battle_cursor_snap: Optional[CursorSnapshot] = None

        # 供 UI 覆盖层显示“按键点击点位”（蓝/橙）
        self._click_markers: dict[str, ClickMarker] = {}

        # 权限检测（辅助功能/注入）
        self._accessibility_trusted: Optional[bool] = None

        # 日志：避免在 tick 循环里刷屏，仅在变化时输出
        self._last_target_active_logged: Optional[bool] = None
        self._ignore_log_last: dict[str, float] = {}
        self._err_log_last: dict[str, float] = {}

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

    def _action_ok(self, action: str, now: float, *, min_interval_s: float = 0.05) -> bool:
        """
        防止同一动作在极短时间内被“EventTap + 轮询兜底”重复触发。
        """
        with self._action_lock:
            last = float(self._action_last_ts.get(action, 0.0))
            if now - last < float(min_interval_s):
                return False
            self._action_last_ts[action] = float(now)
            return True

    def _warn_throttled(self, key: str, now: float, msg: str, *args: object) -> None:
        last = float(self._err_log_last.get(key, 0.0))
        if now - last < 1.0:
            return
        self._err_log_last[key] = float(now)
        self._log.warning(msg, *args)

    def _poll_keyboard(self, now: float) -> None:
        """
        轮询键盘按下状态：
        - 解决某些场景 EventTap 收不到键盘事件导致的“WASD/背包/自定义按键无反应”
        - 与 EventTap 并存时用 _action_ok 做短去重
        """
        try:
            import Quartz
        except Exception:
            # 非 macOS/Quartz 不可用时，禁用轮询兜底
            self._polled_keys_down = set()
            return

        # 不同 macOS/输入设备下，CombinedSessionState/HIDSystemState 的表现可能不同。
        # 为了最大化兼容性，这里同时尝试两种来源（任一为 True 即认为按下）。
        srcs = (
            getattr(Quartz, "kCGEventSourceStateCombinedSessionState", None),
            getattr(Quartz, "kCGEventSourceStateHIDSystemState", None),
        )

        def _key_state(kc: int) -> bool:
            for src in srcs:
                if src is None:
                    continue
                try:
                    if bool(Quartz.CGEventSourceKeyState(src, int(kc))):
                        return True
                except Exception:
                    continue
            return False

        # 读取当前状态（用于门槛判断）
        with self._lock:
            mapping_enabled = bool(self._mapping_enabled)
            target_active = bool(self._target_active)
            mode = self._mode
            cam_lock = bool(self._camera_lock)

        # 1) 移动键（WASD）状态快照：仅用于 Joystick 服务
        move_kcs = (self._kc_move_up, self._kc_move_down, self._kc_move_left, self._kc_move_right)
        self._polled_keys_down = {kc for kc in move_kcs if _key_state(kc)}
        if self._polled_keys_down:
            self._poll_move_ok = True

        def _edge_down(kc: int) -> bool:
            down = _key_state(kc)
            prev = bool(self._poll_prev_down.get(kc, False))
            self._poll_prev_down[kc] = down
            return down and not prev

        # 2) 启用/紧急停止热键：允许在未启用映射时也生效（与 EventTap 行为一致）
        if _edge_down(self._kc_enable) and self._action_ok("hotkey_enable", now):
            self.set_mapping_enabled(not self.is_mapping_enabled())
        if _edge_down(self._kc_panic) and self._action_ok("hotkey_panic", now):
            self.panic()

        # 3) 视角锁定热键（CapsLock 或其他键）
        cam_down = _key_state(self._kc_caps)
        prev_cam = bool(self._poll_prev_down.get(self._kc_caps, False))
        self._poll_prev_down[self._kc_caps] = cam_down
        # 若“视角锁定键”和“背包键”相同，则只执行背包逻辑（背包内已包含锁定切换），避免双重切换。
        if self._kc_caps != self._kc_backpack:
            want_toggle_cam = False
            if self._camera_lock_key_name == "CapsLock":
                # CapsLock 是锁定键：按一次会改变“状态”，这里对状态变化做 toggle
                if cam_down != prev_cam:
                    want_toggle_cam = True
            else:
                # 普通键：仅在按下沿触发一次
                if cam_down and not prev_cam:
                    want_toggle_cam = True
            if want_toggle_cam and mapping_enabled and target_active and self._action_ok("camera_lock", now):
                self.set_camera_lock(not cam_lock)

        # 4) 背包热键：仅在映射启用且目标激活时生效
        if _edge_down(self._kc_backpack) and mapping_enabled and target_active and self._action_ok("backpack", now):
            self.toggle_backpack()

        # 5) 键盘触发的 Tap（开火/开镜/自定义）：在战斗态生效
        if mapping_enabled and target_active and mode == Mode.BATTLE:
            # fire/scope
            t_type, t_val = self._fire_trigger
            if t_type == "key":
                kc = int(t_val)
                if _edge_down(kc) and self._action_ok(f"fire:{kc}", now):
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
            t_type, t_val = self._scope_trigger
            if t_type == "key":
                kc = int(t_val)
                if _edge_down(kc) and self._action_ok(f"scope:{kc}", now):
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

            # customMappings（按下沿触发一次）
            for kc, m in self._custom_by_keycode.items():
                if _edge_down(kc) and self._action_ok(f"custom:{kc}", now):
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
        def _bit(v: bool) -> int:
            return 1 if bool(v) else 0

        secure = _secure_input_enabled()
        now = time.monotonic()
        with self._lock:
            et = set(self._keys_down)
            polled = set(self._polled_keys_down)
            last_kbd_ts = float(self._last_kbd_event_ts)
            return {
                "mapping_enabled": self._mapping_enabled,
                "camera_lock": self._camera_lock,
                "backpack_open": self._backpack_open,
                "target_check_enabled": self._target_check_enabled,
                "target_active": self._target_active,
                "mode": self._mode.value,
                "accessibility_trusted": self._accessibility_trusted,
                "poll_move_ok": bool(self._poll_move_ok),
                "move_eventtap": {
                    "up": _bit(self._kc_move_up in et),
                    "left": _bit(self._kc_move_left in et),
                    "down": _bit(self._kc_move_down in et),
                    "right": _bit(self._kc_move_right in et),
                },
                "move_polled": {
                    "up": _bit(self._kc_move_up in polled),
                    "left": _bit(self._kc_move_left in polled),
                    "down": _bit(self._kc_move_down in polled),
                    "right": _bit(self._kc_move_right in polled),
                },
                "last_kbd_event_age_ms": int(max(0.0, (now - last_kbd_ts) * 1000.0)) if last_kbd_ts > 0.0 else None,
                "secure_input": secure,
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
                    # 背包按钮通常比开火更需要“按住略久一点”才稳定命中
                    hold_ms=max(60, int(self._profile.fire.tapHoldMs)),
                    rrand_px=self._cfg.global_.rrandDefaultPx,
                    pre_release=True,
                    pre_delay_ms=35,
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

        def _throttle(key: str, interval_s: float = 0.8) -> bool:
            last = self._ignore_log_last.get(key, 0.0)
            if now - last < interval_s:
                return False
            self._ignore_log_last[key] = now
            return True

        swallow = False
        with self._lock:
            mapping_enabled = self._mapping_enabled
            target_active = self._target_active
            mode = self._mode

        # 当未启用“目标窗口检测”时，我们会在 tick 里用“前台是否为本程序”来暂停映射，
        # 但 tick 有时间片（默认 0.2s）。为了避免用户刚点击 UI 窗口的那一下被吞掉并触发游戏点击，
        # 这里对“低频交互事件”做一次即时判断：若前台是本程序，则绝不映射/吞输入。
        if mapping_enabled and not self._target_check_enabled and event_type in (
            Quartz.kCGEventKeyDown,
            Quartz.kCGEventKeyUp,
            Quartz.kCGEventFlagsChanged,
            Quartz.kCGEventLeftMouseDown,
            Quartz.kCGEventRightMouseDown,
            Quartz.kCGEventScrollWheel,
        ):
            try:
                if get_frontmost().pid == os.getpid():
                    return False
            except Exception:
                pass

        # 即使映射未启用，也允许热键生效（但不吞输入）。
        if event_type in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp, Quartz.kCGEventFlagsChanged):
            kc = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))

            is_down = event_type != Quartz.kCGEventKeyUp
            prev_down = False
            with self._lock:
                prev_down = kc in self._keys_down
                if is_down:
                    self._keys_down.add(kc)
                else:
                    self._keys_down.discard(kc)
                self._last_kbd_event_ts = now

            if event_type == Quartz.kCGEventKeyDown:
                try:
                    autorepeat = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat))
                except Exception:
                    autorepeat = 0

                if kc == self._kc_enable and not autorepeat and self._action_ok("hotkey_enable", now):
                    with self._lock:
                        self._mapping_enabled = not self._mapping_enabled
                        if not self._mapping_enabled:
                            self._camera_lock = False
                            self._backpack_open = False
                    self._log.info("映射 %s", "启用" if self.is_mapping_enabled() else "禁用")
                elif kc == self._kc_panic and not autorepeat and self._action_ok("hotkey_panic", now):
                    with self._lock:
                        self._mapping_enabled = False
                        self._camera_lock = False
                        self._backpack_open = False
                    self._log.warning("紧急停止：已禁用映射并请求释放所有按住")
                    self._safe_release_all()
                elif (
                    kc == self._kc_backpack
                    and not autorepeat
                    and mapping_enabled
                    and target_active
                    and self._action_ok("backpack", now)
                ):
                    # 背包切换：开 -> 强制自由鼠标；关 -> 自动回战斗且开启视角锁定；并点击背包坐标
                    self.toggle_backpack()
                    swallow = True
                    return True
                elif (
                    # 视角锁定键为普通键（例如 Tab）时，用 keyDown 切换；CapsLock 仍走 flagsChanged。
                    self._camera_lock_key_name != "CapsLock"
                    and kc == self._kc_caps
                    and kc != self._kc_backpack
                    and not autorepeat
                    and mapping_enabled
                    and target_active
                    and self._action_ok("camera_lock", now)
                ):
                    with self._lock:
                        self._camera_lock = not self._camera_lock
                        if self._camera_lock:
                            self._backpack_open = False
                    self._log.info("视角锁定 %s", "开启" if self._camera_lock else "关闭")
                    swallow = True
                    return True
                else:
                    # 内置开火/开镜：若触发键是键盘，则在此处拦截并转为 Tap
                    t_type, t_val = self._fire_trigger
                    if t_type == "key" and kc == int(t_val) and not autorepeat and self._action_ok(f"fire:{int(t_val)}", now):
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
                    if t_type == "key" and kc == int(t_val) and not autorepeat and self._action_ok(f"scope:{int(t_val)}", now):
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
                    if not autorepeat and kc in self._custom_by_keycode and self._action_ok(f"custom:{kc}", now):
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

                # 移动键提示：很多“WASD 无反应”实际是因为未开启战斗态（视角锁定）或目标未激活
                if kc in (self._kc_move_up, self._kc_move_down, self._kc_move_left, self._kc_move_right) and mapping_enabled:
                    # 诊断：记录是否真的收到了 WASD 的 keyDown（只记录首次按下，避免 autorepeat 刷屏）。
                    if not autorepeat and not prev_down:
                        try:
                            g = self._cfg.global_
                            name = "?"
                            if kc == self._kc_move_up:
                                name = str(g.moveUpKey)
                            elif kc == self._kc_move_down:
                                name = str(g.moveDownKey)
                            elif kc == self._kc_move_left:
                                name = str(g.moveLeftKey)
                            elif kc == self._kc_move_right:
                                name = str(g.moveRightKey)
                            self._log.info("捕获移动键按下：%s (kc=%d)", name, kc)
                        except Exception:
                            self._log.info("捕获移动键按下：kc=%d", kc)
                    if not target_active and _throttle("move_key_ignored_target"):
                        self._log.warning("移动键被忽略：目标窗口未激活/未命中（可在配置中关闭目标检测）")
                    elif mode != Mode.BATTLE and _throttle("move_key_ignored_mode"):
                        self._log.warning("移动键被忽略：当前模式=%s（需要开启“视角锁定/战斗模式”）", mode.value)
            # 诊断：移动键抬起（只记录真实 keyUp）
            if event_type == Quartz.kCGEventKeyUp and prev_down and kc in (
                self._kc_move_up,
                self._kc_move_down,
                self._kc_move_left,
                self._kc_move_right,
            ):
                try:
                    g = self._cfg.global_
                    name = "?"
                    if kc == self._kc_move_up:
                        name = str(g.moveUpKey)
                    elif kc == self._kc_move_down:
                        name = str(g.moveDownKey)
                    elif kc == self._kc_move_left:
                        name = str(g.moveLeftKey)
                    elif kc == self._kc_move_right:
                        name = str(g.moveRightKey)
                    self._log.info("捕获移动键抬起：%s (kc=%d)", name, kc)
                except Exception:
                    self._log.info("捕获移动键抬起：kc=%d", kc)

            # CapsLock 使用 flagsChanged 更可靠
            if (
                event_type == Quartz.kCGEventFlagsChanged
                and kc == self._kc_caps
                and mapping_enabled
                and target_active
                and self._action_ok("camera_lock", now)
            ):
                new_state = False
                with self._lock:
                    self._camera_lock = not self._camera_lock
                    new_state = bool(self._camera_lock)
                    if self._camera_lock:
                        self._backpack_open = False
                self._log.info("视角锁定 %s", "开启" if new_state else "关闭")

            # 吞吐策略
            if mapping_enabled and target_active and mode == Mode.BATTLE:
                # 战斗态吞掉键盘输入，避免系统快捷键/其他应用行为
                swallow = True

        elif event_type in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDragged):
            if mapping_enabled and target_active and mode == Mode.BATTLE:
                # 使用 unaccelerated delta（若可用）以尽量让拖动“DPI/手感”与鼠标一致
                dx = None
                dy = None
                ux_f = getattr(Quartz, "kCGMouseEventUnacceleratedDeltaX", None)
                uy_f = getattr(Quartz, "kCGMouseEventUnacceleratedDeltaY", None)
                if ux_f is not None and uy_f is not None:
                    # 有些系统上 unaccelerated delta 是 double field；这里优先取 double，失败再回退 integer。
                    try:
                        dx = float(Quartz.CGEventGetDoubleValueField(event, ux_f))
                        dy = float(Quartz.CGEventGetDoubleValueField(event, uy_f))
                    except Exception:
                        try:
                            dx = float(Quartz.CGEventGetIntegerValueField(event, ux_f))
                            dy = float(Quartz.CGEventGetIntegerValueField(event, uy_f))
                        except Exception:
                            dx = None
                            dy = None
                if dx is None or dy is None:
                    dx = float(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaX))
                    dy = float(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaY))
                else:
                    # 兜底：若 unaccelerated 读到 0 但普通 delta 非 0，则回退普通 delta（避免“移动被吃掉”）。
                    if dx == 0.0 and dy == 0.0:
                        try:
                            dx2 = float(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaX))
                            dy2 = float(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaY))
                            if dx2 != 0.0 or dy2 != 0.0:
                                dx = dx2
                                dy = dy2
                        except Exception:
                            pass
                loc = None
                try:
                    l = Quartz.CGEventGetLocation(event)
                    loc = (float(l.x), float(l.y))
                except Exception:
                    loc = None
                with self._lock:
                    # 记录“真实鼠标”移动时间与位置（用于回中状态机）
                    self._last_mouse_event_ts = now
                    if loc is not None:
                        self._last_mouse_loc = loc
                    # 回中待机：此时不采集轨迹（避免把回中的系统抖动/残余移动计入下一段拖动）
                    if self._cam_recenter_pending:
                        swallow = True
                        return True
                    self._mouse_dx_acc += dx
                    self._mouse_dy_acc += dy
                swallow = True

        elif event_type in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventRightMouseDown):
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
                # 不检测目标窗口时：默认对所有前台应用生效。
                # 但为了让 UI 可用，当前台是本程序时自动暂停映射（否则鼠标点击会被重映射，导致无法操作 UI）。
                active = True
                try:
                    if get_frontmost().pid == os.getpid():
                        active = False
                except Exception:
                    pass
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

        # 键盘轮询兜底：确保在某些 EventTap 收不到键盘事件的场景下仍可工作（WASD/背包/热键/自定义按键）。
        try:
            self._poll_keyboard(now)
        except Exception as e:
            # 不阻塞主循环
            self._log.debug("keyboard poll failed: %s", e)

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
        # 自由鼠标态：默认不做坐标映射，除滚轮映射外。
        # 但背包开关等 Tap 请求需要在自由态也能执行（否则背包永远打不开/关不掉）。
        req: Optional[TapRequest] = None
        with self._lock:
            if self._tap_queue:
                req = self._tap_queue.popleft()
        if req is not None:
            self._service_tap(req)
            return

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
            # 单指限制：滚轮拖动开始前先释放其他按住（camera/joystick）
            self._release_camera_hold()
            self._release_joystick_hold()

            origin = touch_origin or cursor_origin or self._inj.get_cursor_pos()
            r = self._profile.wheel.rrandPx
            if r is None:
                r = self._cfg.global_.rrandDefaultPx
            p0 = random_point(origin, float(r or 0.0), rng=self._rng)
            try:
                self._inj.left_down(p0)
            except Exception as e:
                self._warn_throttled("wheel_down_failed", now, "滚轮按下失败：%s", e)
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
        except Exception as e:
            self._warn_throttled("wheel_drag_failed", now, "滚轮拖动失败：%s", e)
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

        # 2.0) 视角回中待机：当到达边界后，我们会先抬起并进入 pending 状态。
        # 在 pending 期间不采集鼠标轨迹；当检测到“新的鼠标移动事件”到来时，
        # 才在锚点重新按下，确保后续拖动基于“重新按下后的轨迹”，并避免回中反向拖动。
        if self._maybe_start_camera_after_recenter(now):
            return
        # 兜底：用轮询到的 WASD 状态“纠正” EventTap（避免 keyUp 丢失导致“摇杆卡住”）。
        # 注意：部分环境 CGEventSourceKeyState 可能始终返回 False（例如权限/安全输入/设备问题）。
        # 若直接无条件覆盖，会导致 WASD 永远无反应。因此只有在轮询曾经成功检测到移动键时，才信任轮询结果。
        move_kcs = {self._kc_move_up, self._kc_move_down, self._kc_move_left, self._kc_move_right}
        eventtap_move = keys_down & move_kcs
        polled_move = set()
        try:
            polled_move = set(self._polled_keys_down)
        except Exception:
            polled_move = set()
        # 轮询用于“纠正 keyUp 丢失导致的卡键”，但在某些环境下可能间歇性为 0。
        # 若轮询结果为空但 EventTap 仍检测到按下，则必须回退到 EventTap，
        # 否则会出现“摇杆突然无效/完全无反应”。
        if self._poll_move_ok and polled_move:
            move_state = polled_move
        else:
            move_state = eventtap_move or polled_move
        keys_down = (keys_down - move_kcs) | move_state

        want_move = any(
            k in keys_down for k in (self._kc_move_up, self._kc_move_down, self._kc_move_left, self._kc_move_right)
        )

        camera_interval = 1.0 / max(1.0, float(self._profile.scheduler.cameraMinHz))
        joy_interval = 1.0 / max(1.0, float(self._profile.scheduler.joystickMinHz))

        camera_due = (now - self._last_camera_ts) >= camera_interval and (abs(mouse_dx) + abs(mouse_dy)) > 0.0
        joy_due = (now - self._last_joystick_ts) >= joy_interval and want_move

        # 如果摇杆刚刚被按住更新，优先保持一小段时间让游戏端“吃到”触点，
        # 避免 camera 频繁抢占导致摇杆完全不生效。
        if want_move and now < float(self._joy_hold_until_ts):
            camera_due = False

        # 若两者都 due：
        # 旧策略使用“按比例 overdue”会导致 camera 因 interval 更短而长期抢占，摇杆几乎得不到服务（表现为 WASD 无反应）。
        # 这里改为“绝对等待时间”优先，确保 joystickMinHz 真实生效。
        if camera_due and joy_due:
            cam_wait = now - self._last_camera_ts
            joy_wait = now - self._last_joystick_ts
            if joy_wait >= cam_wait:
                self._service_joystick(keys_down)
            else:
                self._service_camera(mouse_dx, mouse_dy)
            return

        if camera_due:
            self._service_camera(mouse_dx, mouse_dy)
            return

        if joy_due:
            self._service_joystick(keys_down)
            return

        # 不 due 时：如果有鼠标输入，依然尽快服务 camera（更贴手）
        tcam = float(self._profile.camera.tcamPx)
        if (not want_move or now >= float(self._joy_hold_until_ts)) and (abs(mouse_dx) + abs(mouse_dy) >= tcam):
            self._service_camera(mouse_dx, mouse_dy)
            return

        # 否则如果在移动，则服务 joystick；若已停止移动，则释放摇杆按住，避免残留
        if want_move:
            self._service_joystick(keys_down)
        else:
            self._release_joystick_hold()

    def _service_tap(self, req: TapRequest) -> None:
        # Tap 前先抬起，避免与 drag 状态混淆；部分按钮需要短暂等待以提高命中率。
        if bool(getattr(req, "pre_release", True)):
            self._safe_release_all()
        delay_ms = int(getattr(req, "pre_delay_ms", 0) or 0)
        if delay_ms > 0:
            time.sleep(max(0.0, float(delay_ms) / 1000.0))
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
        # 视角采用“按住并拖动”（保持一段时间），避免每帧 down->up 造成突兀/无效。
        # 单指限制：执行视角拖动前需要释放摇杆触点。
        self._release_joystick_hold()

        cam = self._profile.camera
        anchor = self._profile.points["cameraAnchor"]

        # 重新读取累计值（避免参数滞后/并发更新）
        with self._lock:
            dx = float(self._mouse_dx_acc)
            dy = float(self._mouse_dy_acc)
            cam_active = bool(self._cam_session.active)
            cur = self._cam_session.touch_pos

        # 小抖动死区
        if (abs(dx) + abs(dy)) < float(cam.tcamPx):
            return

        # 单次最多消耗 thresholdPx 的鼠标位移（阈值越小越丝滑）
        thr = max(1e-3, float(getattr(cam, "thresholdPx", 10.0)))
        l = math.hypot(dx, dy)
        if l > thr:
            s = thr / l
            use_dx = dx * s
            use_dy = dy * s
        else:
            use_dx = dx
            use_dy = dy

        # 扣减已消耗的输入，剩余的下一帧继续处理
        with self._lock:
            self._mouse_dx_acc -= use_dx
            self._mouse_dy_acc -= use_dy

        sx = use_dx
        sy = use_dy * (-1.0 if cam.invertY else 1.0)

        now = time.monotonic()

        # 首次拖动：先在 anchor 处按下并进入“视角按住”session
        if (not cam_active) or (cur is None):
            rr = cam.rrandPx
            if rr is None:
                rr = self._cfg.global_.rrandDefaultPx
            p0 = random_point(anchor, float(rr or 0.0), rng=self._rng)
            try:
                self._inj.left_down(p0)
                # 给极小的按下窗口，让“按住拖动”更稳定
                time.sleep(0.003)
            except Exception as e:
                self._warn_throttled("camera_down_failed", now, "视角按下失败：%s", e)
                self._safe_release_all()
                return
            with self._lock:
                self._cam_session.active = True
                self._cam_session.touch_pos = p0
                self._cam_session.last_drag_ts = now
            cur = p0

        # 计算目标触点位置：从当前触点小幅移动，并限制在 radiusPx 内（围绕 anchor）
        r = float(cam.radiusPx)

        def _inside_circle(p: Point) -> bool:
            dx0 = float(p[0]) - float(anchor[0])
            dy0 = float(p[1]) - float(anchor[1])
            return (dx0 * dx0 + dy0 * dy0) <= (r * r + 1e-6)

        def _clamp_to_circle(p: Point) -> Point:
            dx0 = float(p[0]) - float(anchor[0])
            dy0 = float(p[1]) - float(anchor[1])
            dist0 = math.hypot(dx0, dy0)
            if dist0 <= r or dist0 <= 1e-6:
                return (float(p[0]), float(p[1]))
            s0 = r / dist0
            return (float(anchor[0] + dx0 * s0), float(anchor[1] + dy0 * s0))

        cur = _clamp_to_circle(cur)
        proposed = (float(cur[0] + sx), float(cur[1] + sy))

        # 若触点达到边界：先拖到边界，然后松开 → 回到锚点重新按下。
        # 重要：不要在同一 tick 里继续沿“旧方向剩余位移”拖动，
        # 否则会出现你反馈的“轨迹混乱/不像重新按下后的鼠标轨迹”。
        need_recenter = not _inside_circle(proposed)
        target1 = proposed
        if need_recenter:
            # 线段与圆的交点（cur 在圆内，proposed 在圆外）
            ax = float(cur[0]) - float(anchor[0])
            ay = float(cur[1]) - float(anchor[1])
            dx1 = float(proposed[0]) - float(cur[0])
            dy1 = float(proposed[1]) - float(cur[1])
            a = dx1 * dx1 + dy1 * dy1
            t_hit = None
            if a > 1e-9:
                b = 2.0 * (ax * dx1 + ay * dy1)
                c = ax * ax + ay * ay - r * r
                disc = b * b - 4.0 * a * c
                if disc >= 0.0:
                    sd = math.sqrt(disc)
                    t1 = (-b - sd) / (2.0 * a)
                    t2 = (-b + sd) / (2.0 * a)
                    cand = [t for t in (t1, t2) if 0.0 <= t <= 1.0]
                    if cand:
                        t_hit = min(cand)
            target1 = _clamp_to_circle(proposed) if t_hit is None else (float(cur[0] + dx1 * t_hit), float(cur[1] + dy1 * t_hit))

        # 覆盖层可视化：视角拖动的目标点（蓝/橙由 pressed 决定）
        try:
            with self._lock:
                self._click_markers["camera"] = ClickMarker(
                    x=float(target1[0]),
                    y=float(target1[1]),
                    label="视角拖动",
                    pressed_until_ts=now + 0.20,
                )
        except Exception:
            pass

        try:
            # step_delay_s 稍微给一点节奏，让轨迹更“手指”
            self._inj.drag_smooth(cur, target1, max_step_px=self._profile.scheduler.maxStepPx, step_delay_s=0.001)

            if need_recenter:
                # 到达边界：抬起并进入“回中待机”状态。
                # 关键：不要在同一 tick 里立刻在锚点重新按下，否则 iPhone Mirroring/游戏端可能把
                # “边界→锚点”的跳变误判为反方向拖动。
                try:
                    self._inj.left_up(target1)
                except Exception:
                    pass
                with self._lock:
                    self._cam_session = CameraSession()  # 已抬起：结束本次按住
                    self._mouse_dx_acc = 0.0
                    self._mouse_dy_acc = 0.0
                    self._cam_recenter_pending = True
                    self._cam_recenter_ready = False
                    # 回中目标严格使用锚点坐标（不随机），避免肉眼“回中偏移”
                    self._cam_recenter_pos = (float(anchor[0]), float(anchor[1]))
                    self._cam_recenter_mouse_ts0 = float(self._last_mouse_event_ts)
                # 关键：使用 CGWarpMouseCursorPosition 把系统光标回到锚点（比 mouseMoved 更“硬”），
                # 某些情况下 iPhone Mirroring 仅在光标真实位置改变后才会正确重置下一段拖动基线。
                # 注意：warp 会触发未打标的 mouseMoved 事件，因此必须在 pending 状态下进行。
                try:
                    self._inj.warp((float(anchor[0]), float(anchor[1])))
                except Exception:
                    pass
                with self._lock:
                    if self._cam_recenter_pending:
                        self._cam_recenter_ready = True
                self._last_camera_ts = now
                self._log.info("视角到达边界：已抬起，等待新轨迹后回中")
                return

            with self._lock:
                if self._cam_session.active:
                    self._cam_session.touch_pos = target1
                    self._cam_session.last_drag_ts = now
            self._last_camera_ts = now
        except Exception as e:
            self._warn_throttled("camera_drag_failed", now, "视角拖动失败：%s", e)
            self._safe_release_all()

    def _maybe_start_camera_after_recenter(self, now: float) -> bool:
        """
        视角回中待机状态机：
        - 达到边界时先抬起，进入 pending
        - pending 期间不采集鼠标轨迹
        - 只有当检测到“新的真实鼠标移动事件”到来时，才在锚点重新按下，并清空累计位移
        """
        with self._lock:
            pending = bool(self._cam_recenter_pending)
            pos = self._cam_recenter_pos
            ts0 = float(self._cam_recenter_mouse_ts0)
            last_ts = float(self._last_mouse_event_ts)
            last_loc = self._last_mouse_loc
            ready = bool(self._cam_recenter_ready)

        if (not pending) or (pos is None):
            return False

        # 还在做“回中准备”（warp/同步）时不要启动，避免竞态导致依然反向。
        if not ready:
            return False

        # 还没有新的鼠标移动（保持待机，让摇杆等其他控件继续工作）
        if last_ts <= ts0:
            return False
        # warp 可能会触发一次“位置=锚点”的 mouseMoved；这不应视作用户新轨迹。
        if last_loc is None:
            return False
        if math.hypot(float(last_loc[0]) - float(pos[0]), float(last_loc[1]) - float(pos[1])) < 0.5:
            return False

        # 单指限制：重新按下前释放摇杆（避免立刻被其它服务抢占）
        self._release_joystick_hold()

        try:
            self._inj.left_down(pos)
            # 给极小的按下窗口，让“按住拖动”更稳定
            time.sleep(0.003)
            # 关键：用 0 位移 drag “锚定”新一段拖动起点，强制消费端把基线更新为锚点，
            # 避免在快速甩动时仍把上一段末位置当作本段起点，导致反向跳动。
            try:
                self._inj.left_drag(pos)
            except Exception:
                pass
        except Exception as e:
            self._warn_throttled("camera_recenter_down_failed", now, "视角回中按下失败：%s", e)
            self._safe_release_all()
            with self._lock:
                self._cam_recenter_pending = False
                self._cam_recenter_pos = None
                self._cam_recenter_ready = False
            return True

        with self._lock:
            self._cam_recenter_pending = False
            self._cam_recenter_pos = None
            self._cam_recenter_ready = False
            self._mouse_dx_acc = 0.0
            self._mouse_dy_acc = 0.0
            self._cam_session.active = True
            self._cam_session.touch_pos = pos
            self._cam_session.last_drag_ts = now
        self._last_camera_ts = now
        try:
            self._log.info("视角回中：已重新按下 @ (%.1f, %.1f)", float(pos[0]), float(pos[1]))
        except Exception:
            self._log.info("视角回中：已重新按下")
        return True

    def _service_joystick(self, keys_down: set[int]) -> None:
        # 单指限制：摇杆占用触点时需要先释放“视角按住”
        self._release_camera_hold()

        c0 = self._profile.points["joystickCenter"]
        joy = self._profile.joystick

        vx = 0.0
        vy = 0.0
        if self._kc_move_up in keys_down:
            # Quartz 坐标系 Y 向下：向上移动需要减小 Y
            vy -= 1.0
        if self._kc_move_down in keys_down:
            vy += 1.0
        if self._kc_move_left in keys_down:
            vx -= 1.0
        if self._kc_move_right in keys_down:
            vx += 1.0
        v = normalize((vx, vy))
        if abs(v[0]) + abs(v[1]) <= 1e-9:
            # 没有方向输入：释放摇杆按住
            self._release_joystick_hold()
            return
        now = time.monotonic()

        # 摇杆需要“按住拖动并保持”才能稳定生效：
        # - Down 必须发生在摇杆中心附近
        # - 拖动到 8 方向目标点（由 WASD 组合决定），并保持按住
        with self._lock:
            active = bool(self._joy_session.active)
            cur = self._joy_session.touch_pos
            center = self._joy_session.center_pos

        # 初始化一次“摇杆按住”（Down），并固定本次按住周期内的中心点（支持随机半径）
        if (not active) or (cur is None) or (center is None):
            rr = joy.rrandPx
            if rr is None:
                rr = self._cfg.global_.rrandDefaultPx
            center = random_point(c0, float(rr or 0.0), rng=self._rng)
            try:
                self._inj.left_down(center)
                # 与真实手指一致：Down 后立刻开始向目标方向拖动（不要等到下一 tick）
                cur = center
            except Exception as e:
                self._warn_throttled("joystick_down_failed", now, "摇杆按下失败：%s", e)
                self._safe_release_all()
                return
            with self._lock:
                self._joy_session.active = True
                self._joy_session.center_pos = center
                self._joy_session.touch_pos = cur
                self._joy_session.last_ts = now
            self._last_joystick_ts = now
            self._log.info("摇杆按下 @ (%.1f, %.1f)", float(center[0]), float(center[1]))

        target = add(center, scale(v, float(joy.radiusPx)))

        # 覆盖层可视化：显示目标点。按住期间保持橙色。
        label = "摇杆"
        try:
            g = self._cfg.global_
            parts: list[str] = []
            if self._kc_move_up in keys_down:
                parts.append(str(g.moveUpKey))
            if self._kc_move_down in keys_down:
                parts.append(str(g.moveDownKey))
            if self._kc_move_left in keys_down:
                parts.append(str(g.moveLeftKey))
            if self._kc_move_right in keys_down:
                parts.append(str(g.moveRightKey))
            if parts:
                label = "摇杆：" + "+".join(parts)
        except Exception:
            pass
        with self._lock:
            self._click_markers["joystick"] = ClickMarker(
                x=float(target[0]),
                y=float(target[1]),
                label=label,
                pressed_until_ts=now + 0.35,
            )

        try:
            # 用分段拖动，确保从中心到目标有连续轨迹（更像手指）。
            max_step = max(10.0, float(self._profile.scheduler.maxStepPx))
            self._inj.drag_smooth(cur, target, max_step_px=max_step, step_delay_s=0.0)
            with self._lock:
                if self._joy_session.active:
                    self._joy_session.touch_pos = target
                    self._joy_session.last_ts = now
            self._last_joystick_ts = now
            # 设定最小保持窗口（使用 tauMs 作为“手感保持”参数）。
            hold_s = max(0.04, min(0.20, float(joy.tauMs) / 1000.0))
            self._joy_hold_until_ts = now + hold_s
        except Exception as e:
            self._warn_throttled("joystick_failed", now, "摇杆注入失败：%s", e)
            self._safe_release_all()

    def _release_joystick_hold(self) -> None:
        with self._lock:
            active = bool(self._joy_session.active)
            pos = self._joy_session.touch_pos
            center = self._joy_session.center_pos
            self._joy_session = JoystickSession()
            self._joy_hold_until_ts = 0.0
        if not active:
            return
        try:
            if center is not None:
                self._log.info("摇杆释放 @ (%.1f, %.1f)", float(center[0]), float(center[1]))
            else:
                self._log.info("摇杆释放")
        except Exception:
            pass
        try:
            # 回中再抬起，避免游戏端把“离心抬起”误判为短拖动/无效（更符合真实摇杆手指行为）
            if center is not None and pos is not None:
                try:
                    self._inj.left_drag(center)
                except Exception:
                    pass
                self._inj.left_up(center)
            else:
                self._inj.left_up(pos or self._inj.get_cursor_pos())
        except Exception:
            pass
        # 战斗态下保持光标隐藏，避免释放摇杆导致光标闪现
        try:
            if self.current_mode() == Mode.BATTLE:
                self._inj.hide_cursor()
        except Exception:
            pass

    def _release_camera_hold(self) -> None:
        with self._lock:
            active = bool(self._cam_session.active)
            pos = self._cam_session.touch_pos
            self._cam_session = CameraSession()
        if not active:
            return
        try:
            self._inj.left_up(pos or self._inj.get_cursor_pos())
        except Exception:
            pass
        # 战斗态下保持光标隐藏，避免释放导致光标闪现
        try:
            if self.current_mode() == Mode.BATTLE:
                self._inj.hide_cursor()
        except Exception:
            pass

    def _safe_release_all(self) -> None:
        # 同时清理内部 session 状态，避免“按住残留”
        with self._lock:
            self._wheel = WheelSession()
            self._joy_session = JoystickSession()
            self._cam_session = CameraSession()
            self._joy_hold_until_ts = 0.0
        try:
            self._inj.release_all()
        except Exception:
            pass
        # 战斗态下不应显示系统光标
        try:
            if self.current_mode() == Mode.BATTLE:
                self._inj.hide_cursor()
        except Exception:
            pass
