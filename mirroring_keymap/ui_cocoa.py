from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import objc
from AppKit import (
    NSAlert,
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
        return self

    # --------------------
    # NSApplicationDelegate
    # --------------------

    def applicationDidFinishLaunching_(self, _notification) -> None:
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyRegular)
        NSRunningApplication.currentApplication().activateWithOptions_(1 << 1)  # activateIgnoringOtherApps

        self._create_menu()
        self._create_window()
        self._refresh_profiles_from_current_path()

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
            NSMakeRect(0, 0, 640, 360), style, 2, False
        )
        self._window.setTitle_("Mirroring Keymap (MVP)")
        self._window.center()

        content = self._window.contentView()

        # 配置路径
        self._cfg_path_field = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 310, 420, 24))
        self._cfg_path_field.setStringValue_(str(Path("config.json").resolve()))
        content.addSubview_(self._cfg_path_field)

        btn_choose = NSButton.alloc().initWithFrame_(NSMakeRect(450, 308, 80, 28))
        btn_choose.setTitle_("选择…")
        btn_choose.setBezelStyle_(NSBezelStyleRounded)
        btn_choose.setTarget_(self)
        btn_choose.setAction_("onChooseConfig:")
        content.addSubview_(btn_choose)

        self._btn_open_cfg = NSButton.alloc().initWithFrame_(NSMakeRect(540, 308, 80, 28))
        self._btn_open_cfg.setTitle_("打开")
        self._btn_open_cfg.setBezelStyle_(NSBezelStyleRounded)
        self._btn_open_cfg.setTarget_(self)
        self._btn_open_cfg.setAction_("onOpenConfig:")
        content.addSubview_(self._btn_open_cfg)

        # Profile
        self._profile_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(20, 270, 220, 26), False)
        content.addSubview_(self._profile_popup)

        lbl_profile = NSTextField.alloc().initWithFrame_(NSMakeRect(250, 270, 80, 22))
        lbl_profile.setEditable_(False)
        lbl_profile.setBordered_(False)
        lbl_profile.setDrawsBackground_(False)
        lbl_profile.setStringValue_("Profile")
        content.addSubview_(lbl_profile)

        # Start/Stop
        self._btn_start = NSButton.alloc().initWithFrame_(NSMakeRect(20, 220, 120, 32))
        self._btn_start.setTitle_("启动服务")
        self._btn_start.setBezelStyle_(NSBezelStyleRounded)
        self._btn_start.setTarget_(self)
        self._btn_start.setAction_("onStart:")
        content.addSubview_(self._btn_start)

        self._btn_stop = NSButton.alloc().initWithFrame_(NSMakeRect(150, 220, 120, 32))
        self._btn_stop.setTitle_("停止服务")
        self._btn_stop.setBezelStyle_(NSBezelStyleRounded)
        self._btn_stop.setTarget_(self)
        self._btn_stop.setAction_("onStop:")
        content.addSubview_(self._btn_stop)

        btn_panic = NSButton.alloc().initWithFrame_(NSMakeRect(280, 220, 120, 32))
        btn_panic.setTitle_("紧急停止")
        btn_panic.setBezelStyle_(NSBezelStyleRounded)
        btn_panic.setTarget_(self)
        btn_panic.setAction_("onPanic:")
        content.addSubview_(btn_panic)

        # toggles
        self._chk_enabled = NSButton.alloc().initWithFrame_(NSMakeRect(20, 180, 220, 24))
        self._chk_enabled.setButtonType_(NSButtonTypeSwitch)
        self._chk_enabled.setTitle_("启用映射（吞输入）")
        self._chk_enabled.setTarget_(self)
        self._chk_enabled.setAction_("onToggleEnabled:")
        content.addSubview_(self._chk_enabled)

        self._chk_camera = NSButton.alloc().initWithFrame_(NSMakeRect(20, 150, 220, 24))
        self._chk_camera.setButtonType_(NSButtonTypeSwitch)
        self._chk_camera.setTitle_("视角锁定（战斗态）")
        self._chk_camera.setTarget_(self)
        self._chk_camera.setAction_("onToggleCamera:")
        content.addSubview_(self._chk_camera)

        btn_backpack = NSButton.alloc().initWithFrame_(NSMakeRect(250, 150, 120, 28))
        btn_backpack.setTitle_("背包切换")
        btn_backpack.setBezelStyle_(NSBezelStyleRounded)
        btn_backpack.setTarget_(self)
        btn_backpack.setAction_("onBackpack:")
        content.addSubview_(btn_backpack)

        # pick point
        btn_pick = NSButton.alloc().initWithFrame_(NSMakeRect(20, 110, 120, 28))
        btn_pick.setTitle_("取点（点击）")
        btn_pick.setBezelStyle_(NSBezelStyleRounded)
        btn_pick.setTarget_(self)
        btn_pick.setAction_("onPick:")
        content.addSubview_(btn_pick)

        self._lbl_pick = NSTextField.alloc().initWithFrame_(NSMakeRect(150, 110, 470, 22))
        self._lbl_pick.setEditable_(False)
        self._lbl_pick.setBordered_(False)
        self._lbl_pick.setDrawsBackground_(False)
        self._lbl_pick.setStringValue_("最近取点：-（会复制到剪贴板）")
        content.addSubview_(self._lbl_pick)

        # status
        self._lbl_status = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 20, 600, 80))
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
        path = self._cfg_path()
        try:
            cfg, names = self._app.load_config(path)
        except Exception as e:
            self._profile_popup.removeAllItems()
            self._profile_popup.addItemWithTitle_("(配置无效)")
            self._log.debug("load config failed: %s", e)
            return
        self._profile_popup.removeAllItems()
        for n in names:
            self._profile_popup.addItemWithTitle_(n)

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
                self._refresh_profiles_from_current_path()

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
        s = json.dumps({"x": x, "y": y}, ensure_ascii=False)
        self._lbl_pick.setStringValue_(f"最近取点：{x:.1f}, {y:.1f}（已复制）")
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(s, NSPasteboardTypeString)

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
