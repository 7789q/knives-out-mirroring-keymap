from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import objc
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSAlertSecondButtonReturn,
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBezelStyleRounded,
    NSButton,
    NSButtonTypeMomentaryPushIn,
    NSButtonTypeSwitch,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSOpenPanel,
    NSPasteboard,
    NSPasteboardTypeString,
    NSPopUpButton,
    NSRunningApplication,
    NSTextField,
    NSTimer,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)


class AppDelegate(NSObject):
    def initWithApp_(self, app):  # type: ignore[override]
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self._app = app
        self._log = logging.getLogger("mirroring_keymap.ui")

        self._window = None
        self._timer = None

        self._cfg_path_field = None
        self._profile_popup = None

        self._btn_start = None
        self._btn_stop = None
        self._btn_open_cfg = None

        self._chk_enabled = None
        self._chk_camera = None

        self._lbl_status = None
        self._lbl_pick = None

        self._pick_tap = None
        self._last_pick = None

        # 配置编辑控件引用
        self._cfg_dict = None

        self._global_enable_hotkey = None
        self._global_panic_hotkey = None
        self._global_camera_lock_key = None
        self._global_backpack_key = None
        self._global_rrand_default = None

        self._point_fields = {}  # {"C": (xField, yField), ...}

        self._joy_radius = None
        self._cam_sens = None
        self._cam_invert = None
        self._cam_tcam = None
        self._cam_radius = None

        self._wheel_enabled = None
        self._wheel_d = None
        self._wheel_stop = None

        self._sched_tick = None
        self._sched_cam_min = None
        self._sched_joy_min = None
        self._sched_max_step = None

        self._custom_name = None
        self._custom_key = None
        self._custom_x = None
        self._custom_y = None
        self._custom_hold = None
        self._custom_rrand = None
        self._custom_remove_index = None
        self._custom_list = None
        return self

    # --------------------
    # NSApplicationDelegate
    # --------------------

    def applicationDidFinishLaunching_(self, _notification) -> None:
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyRegular)
        NSRunningApplication.currentApplication().activateWithOptions_(1 << 1)  # activateIgnoringOtherApps

        self._create_menu()
        self._create_window()
        # 首次启动时自动创建默认配置到用户目录，避免 .app Resources 下找不到 config.json
        try:
            self._app.ensure_default_config_exists(self._cfg_path())
        except Exception as e:
            self._log.debug("ensure default config failed: %s", e)
        self.onReloadConfig_(None)

        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.2, self, "onTimer:", None, True
        )

    def applicationWillTerminate_(self, _notification) -> None:
        try:
            self._stop()
        except Exception:
            pass

    # --------------------
    # UI building
    # --------------------

    def _create_menu(self) -> None:
        # 最小菜单，保证 Cmd+Q 等系统行为
        main_menu = NSMenu.alloc().init()
        app_menu_item = NSMenuItem.alloc().init()
        main_menu.addItem_(app_menu_item)

        app_menu = NSMenu.alloc().init()
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", "terminate:", "q")
        app_menu.addItem_(quit_item)
        app_menu_item.setSubmenu_(app_menu)

        NSApplication.sharedApplication().setMainMenu_(main_menu)

    def _create_window(self) -> None:
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
        )
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 920, 650), style, 2, False
        )
        self._window.setTitle_("Mirroring Keymap (MVP)")
        self._window.center()

        content = self._window.contentView()

        def _label(text: str, x: float, y: float, w: float, h: float = 22) -> NSTextField:
            lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
            lbl.setEditable_(False)
            lbl.setBordered_(False)
            lbl.setDrawsBackground_(False)
            lbl.setStringValue_(text)
            return lbl

        # 顶部：配置路径
        self._cfg_path_field = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 610, 650, 24))
        self._cfg_path_field.setStringValue_(self._app.default_config_path())
        content.addSubview_(self._cfg_path_field)

        btn_choose = NSButton.alloc().initWithFrame_(NSMakeRect(680, 608, 100, 28))
        btn_choose.setTitle_("选择…")
        btn_choose.setBezelStyle_(NSBezelStyleRounded)
        btn_choose.setTarget_(self)
        btn_choose.setAction_("onChooseConfig:")
        content.addSubview_(btn_choose)

        self._btn_open_cfg = NSButton.alloc().initWithFrame_(NSMakeRect(790, 608, 100, 28))
        self._btn_open_cfg.setTitle_("打开")
        self._btn_open_cfg.setBezelStyle_(NSBezelStyleRounded)
        self._btn_open_cfg.setTarget_(self)
        self._btn_open_cfg.setAction_("onOpenConfig:")
        content.addSubview_(self._btn_open_cfg)

        # Profile + 保存/重载
        self._profile_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(20, 575, 220, 26), False)
        self._profile_popup.setTarget_(self)
        self._profile_popup.setAction_("onProfileChanged:")
        content.addSubview_(self._profile_popup)
        content.addSubview_(_label("Profile", 250, 575, 80))

        btn_save = NSButton.alloc().initWithFrame_(NSMakeRect(680, 570, 100, 28))
        btn_save.setTitle_("保存配置")
        btn_save.setBezelStyle_(NSBezelStyleRounded)
        btn_save.setTarget_(self)
        btn_save.setAction_("onSaveConfig:")
        content.addSubview_(btn_save)

        btn_reload = NSButton.alloc().initWithFrame_(NSMakeRect(790, 570, 100, 28))
        btn_reload.setTitle_("重载配置")
        btn_reload.setBezelStyle_(NSBezelStyleRounded)
        btn_reload.setTarget_(self)
        btn_reload.setAction_("onReloadConfig:")
        content.addSubview_(btn_reload)

        # Start/Stop
        self._btn_start = NSButton.alloc().initWithFrame_(NSMakeRect(20, 535, 120, 32))
        self._btn_start.setTitle_("启动服务")
        self._btn_start.setBezelStyle_(NSBezelStyleRounded)
        self._btn_start.setTarget_(self)
        self._btn_start.setAction_("onStart:")
        content.addSubview_(self._btn_start)

        self._btn_stop = NSButton.alloc().initWithFrame_(NSMakeRect(150, 535, 120, 32))
        self._btn_stop.setTitle_("停止服务")
        self._btn_stop.setBezelStyle_(NSBezelStyleRounded)
        self._btn_stop.setTarget_(self)
        self._btn_stop.setAction_("onStop:")
        self._btn_stop.setEnabled_(False)
        content.addSubview_(self._btn_stop)

        btn_panic = NSButton.alloc().initWithFrame_(NSMakeRect(280, 535, 120, 32))
        btn_panic.setTitle_("紧急停止")
        btn_panic.setBezelStyle_(NSBezelStyleRounded)
        btn_panic.setTarget_(self)
        btn_panic.setAction_("onPanic:")
        content.addSubview_(btn_panic)

        # toggles
        self._chk_enabled = NSButton.alloc().initWithFrame_(NSMakeRect(20, 500, 220, 24))
        self._chk_enabled.setButtonType_(NSButtonTypeSwitch)
        self._chk_enabled.setTitle_("启用映射（吞输入）")
        self._chk_enabled.setTarget_(self)
        self._chk_enabled.setAction_("onToggleEnabled:")
        content.addSubview_(self._chk_enabled)

        self._chk_camera = NSButton.alloc().initWithFrame_(NSMakeRect(20, 475, 220, 24))
        self._chk_camera.setButtonType_(NSButtonTypeSwitch)
        self._chk_camera.setTitle_("视角锁定（战斗态）")
        self._chk_camera.setTarget_(self)
        self._chk_camera.setAction_("onToggleCamera:")
        content.addSubview_(self._chk_camera)

        btn_backpack = NSButton.alloc().initWithFrame_(NSMakeRect(250, 472, 120, 28))
        btn_backpack.setTitle_("背包切换")
        btn_backpack.setBezelStyle_(NSBezelStyleRounded)
        btn_backpack.setTarget_(self)
        btn_backpack.setAction_("onBackpack:")
        content.addSubview_(btn_backpack)

        # pick point
        btn_pick = NSButton.alloc().initWithFrame_(NSMakeRect(20, 435, 120, 28))
        btn_pick.setTitle_("取点（点击）")
        btn_pick.setBezelStyle_(NSBezelStyleRounded)
        btn_pick.setTarget_(self)
        btn_pick.setAction_("onPick:")
        content.addSubview_(btn_pick)

        self._lbl_pick = NSTextField.alloc().initWithFrame_(NSMakeRect(150, 437, 740, 22))
        self._lbl_pick.setEditable_(False)
        self._lbl_pick.setBordered_(False)
        self._lbl_pick.setDrawsBackground_(False)
        self._lbl_pick.setStringValue_("最近取点：-（会复制到剪贴板）")
        content.addSubview_(self._lbl_pick)

        # --------------------
        # 左侧：点位 + 参数（选中 profile）
        # --------------------
        xL = 20
        xR = 470

        content.addSubview_(_label("点位（屏幕坐标）", xL, 390, 200))
        content.addSubview_(_label("X", xL + 30, 370, 100))
        content.addSubview_(_label("Y", xL + 140, 370, 100))

        self._point_fields = {}
        point_keys = ["C", "A", "F", "S", "I"]
        for idx, key in enumerate(point_keys):
            y = 330 - idx * 30
            content.addSubview_(_label(key, xL, y + 2, 20))
            fx = NSTextField.alloc().initWithFrame_(NSMakeRect(xL + 30, y, 100, 24))
            fy = NSTextField.alloc().initWithFrame_(NSMakeRect(xL + 140, y, 100, 24))
            content.addSubview_(fx)
            content.addSubview_(fy)
            btn_fill = NSButton.alloc().initWithFrame_(NSMakeRect(xL + 250, y - 1, 80, 26))
            btn_fill.setTitle_("填入取点")
            btn_fill.setBezelStyle_(NSBezelStyleRounded)
            btn_fill.setTag_(idx)
            btn_fill.setTarget_(self)
            btn_fill.setAction_("onFillPoint:")
            content.addSubview_(btn_fill)
            self._point_fields[key] = (fx, fy)

        content.addSubview_(_label("参数（当前 Profile）", xL, 190, 200))

        content.addSubview_(_label("摇杆半径", xL, 155, 80))
        self._joy_radius = NSTextField.alloc().initWithFrame_(NSMakeRect(xL + 80, 150, 80, 24))
        content.addSubview_(self._joy_radius)

        content.addSubview_(_label("视角灵敏度", xL + 170, 155, 90))
        self._cam_sens = NSTextField.alloc().initWithFrame_(NSMakeRect(xL + 260, 150, 70, 24))
        content.addSubview_(self._cam_sens)

        self._cam_invert = NSButton.alloc().initWithFrame_(NSMakeRect(xL + 340, 150, 120, 24))
        self._cam_invert.setButtonType_(NSButtonTypeSwitch)
        self._cam_invert.setTitle_("反转Y")
        content.addSubview_(self._cam_invert)

        content.addSubview_(_label("Tcam", xL, 125, 40))
        self._cam_tcam = NSTextField.alloc().initWithFrame_(NSMakeRect(xL + 40, 120, 60, 24))
        content.addSubview_(self._cam_tcam)
        content.addSubview_(_label("视角半径", xL + 110, 125, 60))
        self._cam_radius = NSTextField.alloc().initWithFrame_(NSMakeRect(xL + 170, 120, 60, 24))
        content.addSubview_(self._cam_radius)

        self._wheel_enabled = NSButton.alloc().initWithFrame_(NSMakeRect(xL, 90, 120, 24))
        self._wheel_enabled.setButtonType_(NSButtonTypeSwitch)
        self._wheel_enabled.setTitle_("滚轮映射")
        content.addSubview_(self._wheel_enabled)
        content.addSubview_(_label("D", xL + 120, 95, 20))
        self._wheel_d = NSTextField.alloc().initWithFrame_(NSMakeRect(xL + 140, 90, 60, 24))
        content.addSubview_(self._wheel_d)
        content.addSubview_(_label("Stop(ms)", xL + 210, 95, 60))
        self._wheel_stop = NSTextField.alloc().initWithFrame_(NSMakeRect(xL + 270, 90, 70, 24))
        content.addSubview_(self._wheel_stop)

        content.addSubview_(_label("tick", xL, 65, 35))
        self._sched_tick = NSTextField.alloc().initWithFrame_(NSMakeRect(xL + 35, 60, 45, 24))
        content.addSubview_(self._sched_tick)
        content.addSubview_(_label("camMin", xL + 90, 65, 55))
        self._sched_cam_min = NSTextField.alloc().initWithFrame_(NSMakeRect(xL + 145, 60, 45, 24))
        content.addSubview_(self._sched_cam_min)
        content.addSubview_(_label("joyMin", xL + 200, 65, 55))
        self._sched_joy_min = NSTextField.alloc().initWithFrame_(NSMakeRect(xL + 255, 60, 45, 24))
        content.addSubview_(self._sched_joy_min)
        content.addSubview_(_label("step", xL + 310, 65, 55))
        self._sched_max_step = NSTextField.alloc().initWithFrame_(NSMakeRect(xL + 365, 60, 45, 24))
        content.addSubview_(self._sched_max_step)

        # --------------------
        # 右侧：全局设置 + 自定义点击
        # --------------------
        content.addSubview_(_label("全局设置", xR, 390, 200))

        content.addSubview_(_label("启用热键", xR, 355, 70))
        self._global_enable_hotkey = NSTextField.alloc().initWithFrame_(NSMakeRect(xR + 70, 350, 80, 24))
        content.addSubview_(self._global_enable_hotkey)
        content.addSubview_(_label("紧急热键", xR + 160, 355, 70))
        self._global_panic_hotkey = NSTextField.alloc().initWithFrame_(NSMakeRect(xR + 230, 350, 80, 24))
        content.addSubview_(self._global_panic_hotkey)

        content.addSubview_(_label("视角键", xR, 325, 50))
        self._global_camera_lock_key = NSTextField.alloc().initWithFrame_(NSMakeRect(xR + 50, 320, 100, 24))
        content.addSubview_(self._global_camera_lock_key)
        content.addSubview_(_label("背包键", xR + 160, 325, 50))
        self._global_backpack_key = NSTextField.alloc().initWithFrame_(NSMakeRect(xR + 210, 320, 100, 24))
        content.addSubview_(self._global_backpack_key)

        content.addSubview_(_label("RrandDefault", xR, 295, 90))
        self._global_rrand_default = NSTextField.alloc().initWithFrame_(NSMakeRect(xR + 90, 290, 60, 24))
        content.addSubview_(self._global_rrand_default)

        # 自定义点击
        content.addSubview_(_label("自定义点击（键→Tap）", xR, 260, 200))

        content.addSubview_(_label("名称", xR, 235, 40))
        self._custom_name = NSTextField.alloc().initWithFrame_(NSMakeRect(xR + 40, 230, 160, 24))
        content.addSubview_(self._custom_name)
        content.addSubview_(_label("键", xR + 210, 235, 20))
        self._custom_key = NSTextField.alloc().initWithFrame_(NSMakeRect(xR + 230, 230, 80, 24))
        content.addSubview_(self._custom_key)

        content.addSubview_(_label("X", xR, 205, 15))
        self._custom_x = NSTextField.alloc().initWithFrame_(NSMakeRect(xR + 15, 200, 70, 24))
        content.addSubview_(self._custom_x)
        content.addSubview_(_label("Y", xR + 90, 205, 15))
        self._custom_y = NSTextField.alloc().initWithFrame_(NSMakeRect(xR + 105, 200, 70, 24))
        content.addSubview_(self._custom_y)
        content.addSubview_(_label("hold(ms)", xR + 180, 205, 55))
        self._custom_hold = NSTextField.alloc().initWithFrame_(NSMakeRect(xR + 235, 200, 55, 24))
        content.addSubview_(self._custom_hold)
        content.addSubview_(_label("rrand", xR + 295, 205, 40))
        self._custom_rrand = NSTextField.alloc().initWithFrame_(NSMakeRect(xR + 335, 200, 55, 24))
        content.addSubview_(self._custom_rrand)

        btn_add = NSButton.alloc().initWithFrame_(NSMakeRect(xR + 395, 200, 90, 26))
        btn_add.setTitle_("添加/替换")
        btn_add.setBezelStyle_(NSBezelStyleRounded)
        btn_add.setTarget_(self)
        btn_add.setAction_("onAddCustom:")
        content.addSubview_(btn_add)

        content.addSubview_(_label("删除编号", xR, 170, 60))
        self._custom_remove_index = NSTextField.alloc().initWithFrame_(NSMakeRect(xR + 60, 165, 60, 24))
        content.addSubview_(self._custom_remove_index)
        btn_remove = NSButton.alloc().initWithFrame_(NSMakeRect(xR + 130, 164, 80, 26))
        btn_remove.setTitle_("删除")
        btn_remove.setBezelStyle_(NSBezelStyleRounded)
        btn_remove.setTarget_(self)
        btn_remove.setAction_("onRemoveCustom:")
        content.addSubview_(btn_remove)

        content.addSubview_(_label("当前自定义点击：", xR, 140, 120))
        self._custom_list = NSTextField.alloc().initWithFrame_(NSMakeRect(xR, 70, 420, 70))
        self._custom_list.setEditable_(False)
        self._custom_list.setBordered_(True)
        self._custom_list.setDrawsBackground_(True)
        self._custom_list.setUsesSingleLineMode_(False)
        self._custom_list.setLineBreakMode_(0)
        self._custom_list.setStringValue_("(空)")
        content.addSubview_(self._custom_list)

        # status（底部）
        self._lbl_status = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 10, 870, 50))
        self._lbl_status.setEditable_(False)
        self._lbl_status.setBordered_(False)
        self._lbl_status.setDrawsBackground_(False)
        self._lbl_status.setStringValue_("状态：未启动")
        self._lbl_status.setUsesSingleLineMode_(False)
        self._lbl_status.setLineBreakMode_(0)
        content.addSubview_(self._lbl_status)

        self._window.makeKeyAndOrderFront_(None)

    # --------------------
    # Helpers
    # --------------------

    @objc.python_method
    def _alert(self, title: str, text: str) -> None:
        a = NSAlert.alloc().init()
        a.setMessageText_(title)
        a.setInformativeText_(text)
        a.runModal()

    @objc.python_method
    def _cfg_path(self) -> str:
        return str(self._cfg_path_field.stringValue()).strip()

    @objc.python_method
    def _selected_profile(self) -> Optional[str]:
        name = str(self._profile_popup.titleOfSelectedItem() or "").strip()
        return name or None

    @objc.python_method
    def _refresh_profiles_from_current_path(self) -> None:
        self.onReloadConfig_(None)

    @objc.python_method
    def _safe_float(self, field: NSTextField, default: float) -> float:
        try:
            s = str(field.stringValue()).strip()
            return float(s) if s else default
        except Exception:
            return default

    @objc.python_method
    def _safe_int(self, field: NSTextField, default: int) -> int:
        try:
            s = str(field.stringValue()).strip()
            return int(float(s)) if s else default
        except Exception:
            return default

    @objc.python_method
    def _normalize_key(self, k: str) -> str:
        k = (k or "").strip()
        if len(k) == 1:
            return k.upper()
        return k

    @objc.python_method
    def _profiles_list(self) -> list[dict]:
        if not isinstance(self._cfg_dict, dict):
            return []
        profiles = self._cfg_dict.get("profiles")
        return profiles if isinstance(profiles, list) else []

    @objc.python_method
    def _profile_dict(self, name: Optional[str]) -> Optional[dict]:
        profiles = self._profiles_list()
        if not profiles:
            return None
        if not name:
            p0 = profiles[0]
            return p0 if isinstance(p0, dict) else None
        for p in profiles:
            if isinstance(p, dict) and str(p.get("name") or "") == name:
                return p
        # fallback: first
        p0 = profiles[0]
        return p0 if isinstance(p0, dict) else None

    @objc.python_method
    def _sync_ui_from_cfg(self) -> None:
        if not isinstance(self._cfg_dict, dict):
            return

        g = self._cfg_dict.get("global")
        if not isinstance(g, dict):
            g = {}

        if self._global_enable_hotkey is not None:
            self._global_enable_hotkey.setStringValue_(str(g.get("enableHotkey") or "F8"))
        if self._global_panic_hotkey is not None:
            self._global_panic_hotkey.setStringValue_(str(g.get("panicHotkey") or "F12"))
        if self._global_camera_lock_key is not None:
            self._global_camera_lock_key.setStringValue_(str(g.get("cameraLockKey") or "CapsLock"))
        if self._global_backpack_key is not None:
            self._global_backpack_key.setStringValue_(str(g.get("backpackKey") or "Tab"))
        if self._global_rrand_default is not None:
            self._global_rrand_default.setStringValue_(str(g.get("rrandDefaultPx") if g.get("rrandDefaultPx") is not None else 0))

        p = self._profile_dict(self._selected_profile())
        if not isinstance(p, dict):
            return

        points = p.get("points")
        if not isinstance(points, dict):
            points = {}

        for k, (fx, fy) in self._point_fields.items():
            pt = points.get(k)
            if isinstance(pt, (list, tuple)) and len(pt) == 2:
                fx.setStringValue_(str(pt[0]))
                fy.setStringValue_(str(pt[1]))
            else:
                fx.setStringValue_("")
                fy.setStringValue_("")

        joystick = p.get("joystick") if isinstance(p.get("joystick"), dict) else {}
        camera = p.get("camera") if isinstance(p.get("camera"), dict) else {}
        wheel = p.get("wheel") if isinstance(p.get("wheel"), dict) else {}
        sched = p.get("scheduler") if isinstance(p.get("scheduler"), dict) else {}

        if self._joy_radius is not None:
            self._joy_radius.setStringValue_(str(joystick.get("radiusPx") if joystick.get("radiusPx") is not None else 120))
        if self._cam_sens is not None:
            self._cam_sens.setStringValue_(str(camera.get("sensitivity") if camera.get("sensitivity") is not None else 1.0))
        if self._cam_invert is not None:
            self._cam_invert.setState_(1 if bool(camera.get("invertY")) else 0)
        if self._cam_tcam is not None:
            self._cam_tcam.setStringValue_(str(camera.get("tcamPx") if camera.get("tcamPx") is not None else 3))
        if self._cam_radius is not None:
            self._cam_radius.setStringValue_(str(camera.get("radiusPx") if camera.get("radiusPx") is not None else 80))

        if self._wheel_enabled is not None:
            enabled = wheel.get("enabled")
            self._wheel_enabled.setState_(1 if (enabled if enabled is not None else True) else 0)
        if self._wheel_d is not None:
            self._wheel_d.setStringValue_(str(wheel.get("dPx") if wheel.get("dPx") is not None else 8))
        if self._wheel_stop is not None:
            self._wheel_stop.setStringValue_(str(wheel.get("stopMs") if wheel.get("stopMs") is not None else 120))

        if self._sched_tick is not None:
            self._sched_tick.setStringValue_(str(sched.get("tickHz") if sched.get("tickHz") is not None else 120))
        if self._sched_cam_min is not None:
            self._sched_cam_min.setStringValue_(str(sched.get("cameraMinHz") if sched.get("cameraMinHz") is not None else 50))
        if self._sched_joy_min is not None:
            self._sched_joy_min.setStringValue_(str(sched.get("joystickMinHz") if sched.get("joystickMinHz") is not None else 20))
        if self._sched_max_step is not None:
            self._sched_max_step.setStringValue_(str(sched.get("maxStepPx") if sched.get("maxStepPx") is not None else 6))

        self._refresh_custom_list()

    @objc.python_method
    def _apply_ui_to_cfg(self) -> None:
        if not isinstance(self._cfg_dict, dict):
            return

        g = self._cfg_dict.get("global")
        if not isinstance(g, dict):
            g = {}
            self._cfg_dict["global"] = g

        if self._global_enable_hotkey is not None:
            g["enableHotkey"] = str(self._global_enable_hotkey.stringValue()).strip() or "F8"
        if self._global_panic_hotkey is not None:
            g["panicHotkey"] = str(self._global_panic_hotkey.stringValue()).strip() or "F12"
        if self._global_camera_lock_key is not None:
            g["cameraLockKey"] = str(self._global_camera_lock_key.stringValue()).strip() or "CapsLock"
        if self._global_backpack_key is not None:
            g["backpackKey"] = str(self._global_backpack_key.stringValue()).strip() or "Tab"
        if self._global_rrand_default is not None:
            g["rrandDefaultPx"] = self._safe_float(self._global_rrand_default, 0.0)

        p = self._profile_dict(self._selected_profile())
        if not isinstance(p, dict):
            return

        points = p.get("points")
        if not isinstance(points, dict):
            points = {}
            p["points"] = points

        for k, (fx, fy) in self._point_fields.items():
            x = self._safe_float(fx, 0.0)
            y = self._safe_float(fy, 0.0)
            points[k] = [x, y]

        joystick = p.get("joystick")
        if not isinstance(joystick, dict):
            joystick = {}
            p["joystick"] = joystick
        camera = p.get("camera")
        if not isinstance(camera, dict):
            camera = {}
            p["camera"] = camera
        wheel = p.get("wheel")
        if not isinstance(wheel, dict):
            wheel = {}
            p["wheel"] = wheel
        sched = p.get("scheduler")
        if not isinstance(sched, dict):
            sched = {}
            p["scheduler"] = sched

        if self._joy_radius is not None:
            joystick["radiusPx"] = self._safe_float(self._joy_radius, 120.0)
        if self._cam_sens is not None:
            camera["sensitivity"] = self._safe_float(self._cam_sens, 1.0)
        if self._cam_invert is not None:
            camera["invertY"] = bool(self._cam_invert.state())
        if self._cam_tcam is not None:
            camera["tcamPx"] = self._safe_float(self._cam_tcam, 3.0)
        if self._cam_radius is not None:
            camera["radiusPx"] = self._safe_float(self._cam_radius, 80.0)

        if self._wheel_enabled is not None:
            wheel["enabled"] = bool(self._wheel_enabled.state())
        if self._wheel_d is not None:
            wheel["dPx"] = self._safe_float(self._wheel_d, 8.0)
        if self._wheel_stop is not None:
            wheel["stopMs"] = self._safe_int(self._wheel_stop, 120)

        if self._sched_tick is not None:
            sched["tickHz"] = self._safe_int(self._sched_tick, 120)
        if self._sched_cam_min is not None:
            sched["cameraMinHz"] = self._safe_int(self._sched_cam_min, 50)
        if self._sched_joy_min is not None:
            sched["joystickMinHz"] = self._safe_int(self._sched_joy_min, 20)
        if self._sched_max_step is not None:
            sched["maxStepPx"] = self._safe_float(self._sched_max_step, 6.0)

    @objc.python_method
    def _refresh_custom_list(self) -> None:
        if self._custom_list is None:
            return
        if not isinstance(self._cfg_dict, dict):
            self._custom_list.setStringValue_("(空)")
            return
        lst = self._cfg_dict.get("customMappings")
        if not isinstance(lst, list) or not lst:
            self._custom_list.setStringValue_("(空)")
            return

        lines = []
        for i, m in enumerate(lst, start=1):
            if not isinstance(m, dict):
                continue
            name = str(m.get("name") or "")
            key = str(m.get("key") or "")
            pt = m.get("point")
            hold = m.get("tapHoldMs", 30)
            rrand = m.get("rrandPx", None)
            lines.append(f"{i}. {name} | key={key} | point={pt} | hold={hold} | rrand={rrand}")
        self._custom_list.setStringValue_("\n".join(lines) if lines else "(空)")

    # --------------------
    # Actions
    # --------------------

    def onChooseConfig_(self, _sender) -> None:
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setAllowedFileTypes_(["json"])
        if panel.runModal() == 1:
            url = panel.URL()
            if url is not None:
                path = str(url.path())
                self._cfg_path_field.setStringValue_(path)
                try:
                    self._app.ensure_default_config_exists(path)
                except Exception:
                    pass
                self.onReloadConfig_(None)

    def onOpenConfig_(self, _sender) -> None:
        if not self._cfg_path():
            return
        try:
            self._app.open_in_editor(self._cfg_path())
        except Exception as e:
            self._alert("打开失败", str(e))

    def onStart_(self, _sender) -> None:
        try:
            self._app.start(self._cfg_path(), self._selected_profile())
        except Exception as e:
            self._alert("启动失败", str(e))

    def onStop_(self, _sender) -> None:
        self._stop()

    @objc.python_method
    def _stop(self) -> None:
        try:
            self._app.stop()
        except Exception as e:
            self._alert("停止失败", str(e))

    def onPanic_(self, _sender) -> None:
        try:
            self._app.panic()
        except Exception as e:
            self._alert("紧急停止失败", str(e))

    def onToggleEnabled_(self, _sender) -> None:
        try:
            enabled = bool(self._chk_enabled.state())
            self._app.set_mapping_enabled(enabled)
        except Exception as e:
            self._alert("设置失败", str(e))

    def onToggleCamera_(self, _sender) -> None:
        try:
            enabled = bool(self._chk_camera.state())
            self._app.set_camera_lock(enabled)
        except Exception as e:
            self._alert("设置失败", str(e))

    def onBackpack_(self, _sender) -> None:
        try:
            self._app.toggle_backpack()
        except Exception as e:
            self._alert("背包切换失败", str(e))

    def onPick_(self, _sender) -> None:
        if self._pick_tap is not None:
            return

        try:
            import Quartz
        except Exception as e:
            self._alert("取点失败", f"无法导入 Quartz：{e}")
            return

        # 监听一次左键按下，取点并复制到剪贴板
        result = {}

        def _cb(_proxy, event_type, event, _refcon):
            if event_type == Quartz.kCGEventLeftMouseDown:
                loc = Quartz.CGEventGetLocation(event)
                result["x"] = float(loc.x)
                result["y"] = float(loc.y)
                try:
                    Quartz.CGEventTapEnable(tap, False)
                except Exception:
                    pass
                try:
                    Quartz.CFRunLoopRemoveSource(Quartz.CFRunLoopGetCurrent(), src, Quartz.kCFRunLoopCommonModes)
                except Exception:
                    pass
                self._pick_tap = None
                self._on_picked(result["x"], result["y"])
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
            self._alert("取点失败", "创建 EventTap 失败：请检查 Input Monitoring / Accessibility 权限。")
            return
        src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        self._pick_tap = (tap, src)

        self._lbl_pick.setStringValue_("请在屏幕上点击一次以取点…")

    @objc.python_method
    def _on_picked(self, x: float, y: float) -> None:
        self._last_pick = (x, y)
        s = json.dumps({"x": x, "y": y}, ensure_ascii=False)
        self._lbl_pick.setStringValue_(f"最近取点：{x:.1f}, {y:.1f}（已复制）")
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(s, NSPasteboardTypeString)

    def onProfileChanged_(self, _sender) -> None:
        # 切换 profile 时仅刷新表单，不强制重载文件
        try:
            self._sync_ui_from_cfg()
        except Exception:
            pass

    def onReloadConfig_(self, _sender) -> None:
        path = self._cfg_path()
        try:
            self._app.ensure_default_config_exists(path)
        except Exception:
            pass

        try:
            self._cfg_dict = self._app.load_config_dict(path)
        except Exception as e:
            self._cfg_dict = None
            self._alert("加载配置失败", str(e))
            return

        # 刷新 profile 列表
        profiles = self._profiles_list()
        names: list[str] = []
        for i, p in enumerate(profiles):
            if not isinstance(p, dict):
                continue
            nm = str(p.get("name") or f"Profile {i+1}")
            names.append(nm)
            p["name"] = nm

        if not names:
            names = ["Default"]
            if isinstance(self._cfg_dict, dict):
                self._cfg_dict["profiles"] = [{"name": "Default", "points": {}}]

        current = self._selected_profile()
        self._profile_popup.removeAllItems()
        for n in names:
            self._profile_popup.addItemWithTitle_(n)
        if current and current in names:
            self._profile_popup.selectItemWithTitle_(current)
        else:
            self._profile_popup.selectItemAtIndex_(0)

        # 同步表单
        self._sync_ui_from_cfg()

        # 尝试严格校验（不阻塞编辑）
        try:
            self._app.load_config(path)
        except Exception as e:
            self._log.debug("config validate failed: %s", e)

    def onSaveConfig_(self, _sender) -> None:
        if not isinstance(self._cfg_dict, dict):
            self.onReloadConfig_(None)
        if not isinstance(self._cfg_dict, dict):
            return

        try:
            self._apply_ui_to_cfg()
            self._app.save_config_dict(self._cfg_path(), self._cfg_dict)
        except Exception as e:
            self._alert("保存失败", str(e))
            return

        # 保存后做一次严格校验，提示更明确
        try:
            self._app.load_config(self._cfg_path())
        except Exception as e:
            self._alert("保存成功但配置无效", str(e))
            return

        self.onReloadConfig_(None)

    def onFillPoint_(self, sender) -> None:
        if self._last_pick is None:
            self._alert("无法填入", "请先点击“取点（点击）”获取坐标。")
            return
        tag = int(sender.tag())
        keys = ["C", "A", "F", "S", "I"]
        if tag < 0 or tag >= len(keys):
            return
        key = keys[tag]
        fields = self._point_fields.get(key)
        if not fields:
            return
        fx, fy = fields
        x, y = self._last_pick
        fx.setStringValue_(f"{x:.1f}")
        fy.setStringValue_(f"{y:.1f}")

    def onAddCustom_(self, _sender) -> None:
        if not isinstance(self._cfg_dict, dict):
            self.onReloadConfig_(None)
        if not isinstance(self._cfg_dict, dict):
            return

        name = str(self._custom_name.stringValue() if self._custom_name else "").strip() or "Custom"
        key = self._normalize_key(str(self._custom_key.stringValue() if self._custom_key else ""))
        if not key:
            self._alert("新增失败", "请填写触发键（例如：E / R / Space / 1）。")
            return

        x = self._safe_float(self._custom_x, 0.0)
        y = self._safe_float(self._custom_y, 0.0)
        hold = self._safe_int(self._custom_hold, 30)
        rrand_raw = str(self._custom_rrand.stringValue() if self._custom_rrand else "").strip()
        rrand = float(rrand_raw) if rrand_raw else None

        mapping = {
            "name": name,
            "key": key,
            "type": "tap",
            "point": [x, y],
            "tapHoldMs": hold,
            "rrandPx": rrand,
        }

        lst = self._cfg_dict.get("customMappings")
        if not isinstance(lst, list):
            lst = []
            self._cfg_dict["customMappings"] = lst

        key_u = key.upper()
        existing_idx = None
        existing_name = None
        for i, m in enumerate(lst):
            if not isinstance(m, dict):
                continue
            k = self._normalize_key(str(m.get("key") or ""))
            if k.upper() == key_u:
                existing_idx = i
                existing_name = str(m.get("name") or "")
                break

        if existing_idx is not None:
            a = NSAlert.alloc().init()
            a.setMessageText_("按键已存在")
            a.setInformativeText_(f"按键 {key} 已绑定到「{existing_name}」。是否替换？")
            a.addButtonWithTitle_("替换")
            a.addButtonWithTitle_("取消")
            ret = a.runModal()
            if ret != NSAlertFirstButtonReturn:
                return
            lst[existing_idx] = mapping
        else:
            lst.append(mapping)

        self.onSaveConfig_(None)

    def onRemoveCustom_(self, _sender) -> None:
        if not isinstance(self._cfg_dict, dict):
            self.onReloadConfig_(None)
        if not isinstance(self._cfg_dict, dict):
            return

        idx = self._safe_int(self._custom_remove_index, -1)
        lst = self._cfg_dict.get("customMappings")
        if not isinstance(lst, list) or not lst:
            return
        # UI 使用 1-based 编号
        real = idx - 1
        if real < 0 or real >= len(lst):
            self._alert("删除失败", "编号无效。")
            return
        lst.pop(real)
        self.onSaveConfig_(None)

    # --------------------
    # Timer
    # --------------------

    def onTimer_(self, _timer) -> None:
        snap = self._app.snapshot()
        if not snap.get("running"):
            self._lbl_status.setStringValue_("状态：未启动")
            self._btn_start.setEnabled_(True)
            self._btn_stop.setEnabled_(False)
            return

        self._btn_start.setEnabled_(False)
        self._btn_stop.setEnabled_(True)

        txt = (
            f"状态：运行中\n"
            f"config: {snap.get('config')}\n"
            f"profile: {snap.get('profile')}\n"
            f"mode: {snap.get('mode')} | target_active: {snap.get('target_active')}\n"
            f"enabled: {snap.get('mapping_enabled')} | camera_lock: {snap.get('camera_lock')} | backpack: {snap.get('backpack_open')}"
        )
        self._lbl_status.setStringValue_(txt)

        # 同步复选框（避免 UI 和热键状态不一致）
        try:
            self._chk_enabled.setState_(1 if snap.get("mapping_enabled") else 0)
            self._chk_camera.setState_(1 if snap.get("camera_lock") else 0)
        except Exception:
            pass
