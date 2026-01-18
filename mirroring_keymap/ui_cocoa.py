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
    NSColor,
    NSFont,
    NSBezierPath,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSOpenPanel,
    NSPasteboard,
    NSPasteboardTypeString,
    NSPopUpButton,
    NSRunningApplication,
    NSScreen,
    NSScrollView,
    NSTabView,
    NSTabViewItem,
    NSTextField,
    NSTextView,
    NSTimer,
    NSView,
    NSViewHeightSizable,
    NSViewWidthSizable,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSString


class _MarkerView(NSView):
    def initWithFrame_(self, frame):  # type: ignore[override]
        self = objc.super(_MarkerView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._markers = []
        return self

    @objc.python_method
    def set_markers(self, markers: list[dict]) -> None:
        self._markers = markers
        try:
            self.setNeedsDisplay_(True)
        except Exception:
            pass

    def isFlipped(self):  # type: ignore[override]
        # 使用 AppKit 默认坐标系（原点左下，Y 向上）。
        # 由 _MarkerOverlay.update 负责把 Quartz 全局坐标（原点左上，Y 向下）
        # 转换为本视图坐标，避免不同 pyobjc 版本下 flipped 行为差异导致偏移。
        return False

    def drawRect_(self, rect) -> None:  # type: ignore[override]
        # 透明覆盖层，仅绘制点位标记
        _ = rect
        try:
            for m in self._markers:
                x = float(m.get("x", 0.0))
                y = float(m.get("y", 0.0))
                r = float(m.get("r", 7.0))
                color = m.get("color") or NSColor.systemRedColor()
                label = str(m.get("label") or "")

                # 外圈
                NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.15).setFill()
                bg = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x - r - 3, y - r - 3, (r + 3) * 2, (r + 3) * 2))
                bg.fill()

                color.colorWithAlphaComponent_(0.35).setFill()
                path = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x - r, y - r, r * 2, r * 2))
                path.fill()
                color.setStroke()
                path.setLineWidth_(2.0)
                path.stroke()

                # 十字
                NSColor.whiteColor().colorWithAlphaComponent_(0.8).setStroke()
                cross = NSBezierPath.bezierPath()
                cross.moveToPoint_((x - (r + 6), y))
                cross.lineToPoint_((x + (r + 6), y))
                cross.moveToPoint_((x, y - (r + 6)))
                cross.lineToPoint_((x, y + (r + 6)))
                cross.setLineWidth_(1.0)
                cross.stroke()

                if label:
                    attrs = {
                        "NSFont": NSFont.systemFontOfSize_(12),
                        "NSColor": NSColor.whiteColor().colorWithAlphaComponent_(0.85),
                    }
                    try:
                        NSString.stringWithString_(label).drawAtPoint_withAttributes_((x + r + 8, y + r + 6), attrs)
                    except Exception:
                        pass
        except Exception:
            # 绘制失败不应影响主程序
            pass


class _MarkerOverlay:
    def __init__(self) -> None:
        self._windows: list[NSWindow] = []
        self._views: list[_MarkerView] = []
        # Quartz 坐标系下的每屏 bounds（origin=左上，Y 向下）
        self._bounds: list[tuple[float, float, float, float]] = []

    def show(self) -> None:
        if self._windows:
            for w in self._windows:
                try:
                    w.orderFront_(None)
                except Exception:
                    pass
            return

        try:
            import Quartz
        except Exception:
            Quartz = None  # type: ignore[assignment]

        for screen in NSScreen.screens() or []:
            frame = screen.frame()
            w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(frame, NSWindowStyleMaskBorderless, 2, False)
            w.setOpaque_(False)
            w.setBackgroundColor_(NSColor.clearColor())
            w.setIgnoresMouseEvents_(True)
            try:
                w.setLevel_(10_000)  # 近似浮层（避免依赖常量在不同 pyobjc 版本不一致）
            except Exception:
                pass
            try:
                w.setCollectionBehavior_(
                    NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
                )
            except Exception:
                pass

            v = _MarkerView.alloc().initWithFrame_(NSMakeRect(0, 0, frame.size.width, frame.size.height))
            w.setContentView_(v)
            w.orderFront_(None)
            self._windows.append(w)
            self._views.append(v)
            # 记录该屏在 Quartz 下的 bounds，用于把“取点/注入”的坐标正确映射到覆盖层
            bx, by, bw, bh = 0.0, 0.0, float(frame.size.width), float(frame.size.height)
            try:
                if Quartz is not None:
                    dd = screen.deviceDescription()
                    did = dd.get("NSScreenNumber") if hasattr(dd, "get") else None
                    if did is not None:
                        b = Quartz.CGDisplayBounds(int(did))
                        bx = float(b.origin.x)
                        by = float(b.origin.y)
                        bw = float(b.size.width)
                        bh = float(b.size.height)
            except Exception:
                pass
            self._bounds.append((bx, by, bw, bh))

    def hide(self) -> None:
        for w in self._windows:
            try:
                w.orderOut_(None)
            except Exception:
                pass
        self._windows = []
        self._views = []
        self._bounds = []

    def update(self, markers_global: list[dict]) -> None:
        if not self._windows:
            return

        # 按屏幕分发坐标（Quartz 全局 -> 每屏局部 -> AppKit 本地）
        per_view: list[list[dict]] = [[] for _ in self._views]
        for m in markers_global:
            try:
                gx = float(m.get("x", 0.0))
                gy = float(m.get("y", 0.0))
            except Exception:
                continue
            for idx, (bx, by, bw, bh) in enumerate(self._bounds):
                # Quartz 坐标：origin=左上，Y 向下
                if gx >= bx and gx <= bx + bw and gy >= by and gy <= by + bh:
                    mm = dict(m)
                    mm["x"] = gx - bx
                    # 转换到 AppKit 视图坐标：origin=左下，Y 向上
                    mm["y"] = bh - (gy - by)
                    per_view[idx].append(mm)
                    break

        for v, lst in zip(self._views, per_view):
            v.set_markers(lst)


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
        self._btn_logs = None

        self._chk_enabled = None
        self._chk_camera = None
        self._chk_overlay = None

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
        self._global_move_up_key = None
        self._global_move_down_key = None
        self._global_move_left_key = None
        self._global_move_right_key = None
        self._global_fire_key = None
        self._global_scope_key = None
        self._global_rrand_default = None

        self._point_fields = {}  # {"C": (xField, yField), ...}

        self._joy_radius = None
        self._cam_thresh = None
        self._cam_invert = None
        self._cam_tcam = None
        self._cam_radius = None

        self._wheel_enabled = None
        self._wheel_d = None
        self._wheel_stop = None
        self._wheel_anchor_x = None
        self._wheel_anchor_y = None

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

        # target window config
        self._tw_title_hint = None
        self._tw_pid = None
        self._btn_tw_detect = None
        self._tw_disable = None

        # 日志窗口
        self._log_window = None
        self._log_text = None

        # 点位标记覆盖层
        self._overlay = None
        self._tab = None

        # 记录配置文件 mtime，用于提示“外部修改未生效/需要重载”
        self._cfg_mtime = None
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

        # 如果上次保存时开启了覆盖层，则启动时自动恢复
        try:
            if self._chk_overlay is not None and bool(self._chk_overlay.state()):
                self._ensure_overlay()
                self._update_overlay()
        except Exception:
            pass

        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.2, self, "onTimer:", None, True
        )

    def applicationWillTerminate_(self, _notification) -> None:
        # 退出前尽量保存 UI 配置（用户诉求：一切以 UI 为准，无需手工编辑 config.json）
        try:
            if isinstance(self._cfg_dict, dict):
                self._apply_ui_to_cfg()
                self._app.save_config_dict(self._cfg_path(), self._cfg_dict)
        except Exception as e:
            try:
                self._log.debug("persist config on quit failed: %s", e)
            except Exception:
                pass
        try:
            self._stop()
        except Exception:
            pass
        try:
            if self._overlay is not None:
                self._overlay.hide()
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
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("退出", "terminate:", "q")
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
        self._window.setTitle_("荒野行动按键映射（MVP）")
        self._window.center()

        # 主界面支持低分辨率：整个窗口内容放进 ScrollView，避免缩小时控件不可见。
        base = self._window.contentView()
        main_scroll = NSScrollView.alloc().initWithFrame_(base.bounds())
        main_scroll.setHasVerticalScroller_(True)
        main_scroll.setHasHorizontalScroller_(True)
        try:
            main_scroll.setAutohidesScrollers_(True)
        except Exception:
            pass
        main_scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 920, 650))
        main_scroll.setDocumentView_(content)
        self._window.setContentView_(main_scroll)

        def _label(text: str, x: float, y: float, w: float, h: float = 22) -> NSTextField:
            lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
            lbl.setEditable_(False)
            lbl.setBordered_(False)
            lbl.setDrawsBackground_(False)
            lbl.setStringValue_(text)
            return lbl

        # 按键下拉选项：不再使用“录入”，避免焦点/输入框占用导致误操作。
        # 选项严格对应当前引擎支持的键名（keycodes.KEYCODES + MouseLeft/MouseRight）。
        try:
            from mirroring_keymap.macos.keycodes import KEYCODES
        except Exception:
            KEYCODES = {}  # type: ignore[assignment]

        def _key_sort_key(name: str) -> tuple:
            # F1..F12
            if name.startswith("F") and name[1:].isdigit():
                return (0, int(name[1:]))
            # letters
            if len(name) == 1 and name.isalpha():
                return (1, name)
            # digits
            if len(name) == 1 and name.isdigit():
                return (2, name)
            # common specials (manual order)
            special = {
                "Tab": 0,
                "Space": 1,
                "Escape": 2,
                "CapsLock": 3,
                "Shift": 4,
                "Control": 5,
                "Option": 6,
                "LeftArrow": 7,
                "RightArrow": 8,
                "UpArrow": 9,
                "DownArrow": 10,
            }
            return (3, special.get(name, 999), name)

        _keyboard_keys = sorted([str(k) for k in getattr(KEYCODES, "keys", lambda: [])()], key=_key_sort_key)
        _keys_with_mouse = ["MouseLeft", "MouseRight"] + _keyboard_keys

        def _key_popup(rect, *, include_mouse: bool) -> NSPopUpButton:
            p = NSPopUpButton.alloc().initWithFrame_pullsDown_(rect, False)
            titles = _keys_with_mouse if include_mouse else _keyboard_keys
            try:
                p.addItemsWithTitles_(titles)
            except Exception:
                # 某些 pyobjc 版本下 addItemsWithTitles_ 可能不稳定，退化为逐个 addItemWithTitle_
                for t in titles:
                    try:
                        p.addItemWithTitle_(t)
                    except Exception:
                        pass
            return p

        # 顶部：不暴露/不选择配置文件/配置档（设置以 UI 为准，自动持久化到用户目录）
        content.addSubview_(_label("设置会自动保存（无需选择配置文件/配置档）", 20, 620, 520))
        try:
            content.addSubview_(_label(f"保存位置：{self._cfg_path()}", 20, 592, 640))
        except Exception:
            content.addSubview_(_label("保存位置：~/Library/Application Support/MirroringKeymap/config.json", 20, 592, 640))

        btn_save = NSButton.alloc().initWithFrame_(NSMakeRect(680, 588, 100, 28))
        btn_save.setTitle_("保存设置")
        btn_save.setBezelStyle_(NSBezelStyleRounded)
        btn_save.setTarget_(self)
        btn_save.setAction_("onSaveConfig:")
        content.addSubview_(btn_save)

        btn_reload = NSButton.alloc().initWithFrame_(NSMakeRect(790, 588, 100, 28))
        btn_reload.setTitle_("重新加载")
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

        self._btn_logs = NSButton.alloc().initWithFrame_(NSMakeRect(410, 535, 120, 32))
        self._btn_logs.setTitle_("查看日志")
        self._btn_logs.setBezelStyle_(NSBezelStyleRounded)
        self._btn_logs.setTarget_(self)
        self._btn_logs.setAction_("onShowLogs:")
        content.addSubview_(self._btn_logs)

        # toggles
        self._chk_enabled = NSButton.alloc().initWithFrame_(NSMakeRect(20, 500, 220, 24))
        self._chk_enabled.setButtonType_(NSButtonTypeSwitch)
        self._chk_enabled.setTitle_("启用映射（吞输入）")
        self._chk_enabled.setState_(1)  # 默认启用：用户点击“开始”后可立即使用
        self._chk_enabled.setTarget_(self)
        self._chk_enabled.setAction_("onToggleEnabled:")
        content.addSubview_(self._chk_enabled)

        self._chk_camera = NSButton.alloc().initWithFrame_(NSMakeRect(20, 475, 220, 24))
        self._chk_camera.setButtonType_(NSButtonTypeSwitch)
        self._chk_camera.setTitle_("视角锁定（战斗态）")
        self._chk_camera.setState_(1)  # 默认开启：避免“WASD 无反应”
        self._chk_camera.setTarget_(self)
        self._chk_camera.setAction_("onToggleCamera:")
        content.addSubview_(self._chk_camera)

        self._chk_overlay = NSButton.alloc().initWithFrame_(NSMakeRect(250, 500, 220, 24))
        self._chk_overlay.setButtonType_(NSButtonTypeSwitch)
        self._chk_overlay.setTitle_("显示点位标记（调试）")
        self._chk_overlay.setState_(1)  # 默认显示：便于看见点击/摇杆标识
        self._chk_overlay.setTarget_(self)
        self._chk_overlay.setAction_("onToggleOverlay:")
        content.addSubview_(self._chk_overlay)

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
        # 分页设置：把“点位/参数/自定义”按类别分开，避免挤在同一屏；
        # 同时每页使用 ScrollView，窗口缩小后仍可滚动查看全部设置。
        self._tab = NSTabView.alloc().initWithFrame_(NSMakeRect(20, 70, 870, 355))
        self._tab.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        content.addSubview_(self._tab)

        def _make_scroll(doc_w: float, doc_h: float):
            scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 870, 355))
            scroll.setHasVerticalScroller_(True)
            scroll.setHasHorizontalScroller_(True)
            try:
                scroll.setAutohidesScrollers_(True)
            except Exception:
                pass
            doc = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, doc_w, doc_h))
            scroll.setDocumentView_(doc)
            scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            return scroll, doc

        def _add_tab(identifier: str, title: str, doc_w: float, doc_h: float):
            scroll, doc = _make_scroll(doc_w, doc_h)
            item = NSTabViewItem.alloc().initWithIdentifier_(identifier)
            item.setLabel_(title)
            item.setView_(scroll)
            self._tab.addTabViewItem_(item)
            return doc

        # --------------------
        # Tab 1: 点位
        # --------------------
        doc_points = _add_tab("points", "点位", 860, 360)
        doc_points.addSubview_(_label("点位 + 对应按键（屏幕全局坐标）", 20, 325, 300))

        # 全局热键/通用参数（放在同一页，避免“按键页”和“点位页”来回切）
        doc_points.addSubview_(_label("启用", 520, 327, 35))
        self._global_enable_hotkey = _key_popup(NSMakeRect(555, 320, 70, 26), include_mouse=False)
        doc_points.addSubview_(self._global_enable_hotkey)

        doc_points.addSubview_(_label("紧急", 635, 327, 35))
        self._global_panic_hotkey = _key_popup(NSMakeRect(670, 320, 70, 26), include_mouse=False)
        doc_points.addSubview_(self._global_panic_hotkey)

        doc_points.addSubview_(_label("随机", 750, 327, 35))
        self._global_rrand_default = NSTextField.alloc().initWithFrame_(NSMakeRect(785, 322, 55, 24))
        doc_points.addSubview_(self._global_rrand_default)

        doc_points.addSubview_(_label("X", 170, 295, 20))
        doc_points.addSubview_(_label("Y", 290, 295, 20))
        doc_points.addSubview_(_label("键", 520, 295, 18))
        doc_points.addSubview_(_label("上", 545, 295, 18))
        doc_points.addSubview_(_label("下", 595, 295, 18))
        doc_points.addSubview_(_label("左", 645, 295, 18))
        doc_points.addSubview_(_label("右", 695, 295, 18))

        self._point_fields = {}
        point_defs = [
            ("joystickCenter", "摇杆中心"),
            ("cameraAnchor", "视角锚点"),
            ("fire", "开火点击"),
            ("scope", "开镜点击"),
            ("backpack", "背包按钮"),
        ]
        for idx, (key, label) in enumerate(point_defs):
            y = 255 - idx * 42
            doc_points.addSubview_(_label(label, 20, y + 2, 120))
            fx = NSTextField.alloc().initWithFrame_(NSMakeRect(160, y, 110, 24))
            fy = NSTextField.alloc().initWithFrame_(NSMakeRect(280, y, 110, 24))
            doc_points.addSubview_(fx)
            doc_points.addSubview_(fy)
            btn_fill = NSButton.alloc().initWithFrame_(NSMakeRect(405, y - 1, 100, 26))
            btn_fill.setTitle_("填入取点")
            btn_fill.setBezelStyle_(NSBezelStyleRounded)
            btn_fill.setTag_(idx)
            btn_fill.setTarget_(self)
            btn_fill.setAction_("onFillPoint:")
            doc_points.addSubview_(btn_fill)
            self._point_fields[key] = (fx, fy)

            # 对应按键设置：跟随点位展示（用户诉求：按键设置与点位设置合并）
            if key == "joystickCenter":
                self._global_move_up_key = _key_popup(NSMakeRect(540, y - 1, 55, 26), include_mouse=False)
                self._global_move_down_key = _key_popup(NSMakeRect(595, y - 1, 55, 26), include_mouse=False)
                self._global_move_left_key = _key_popup(NSMakeRect(650, y - 1, 55, 26), include_mouse=False)
                self._global_move_right_key = _key_popup(NSMakeRect(705, y - 1, 55, 26), include_mouse=False)
                doc_points.addSubview_(self._global_move_up_key)
                doc_points.addSubview_(self._global_move_down_key)
                doc_points.addSubview_(self._global_move_left_key)
                doc_points.addSubview_(self._global_move_right_key)
            elif key == "cameraAnchor":
                self._global_camera_lock_key = _key_popup(NSMakeRect(540, y - 1, 160, 26), include_mouse=False)
                doc_points.addSubview_(self._global_camera_lock_key)
            elif key == "fire":
                self._global_fire_key = _key_popup(NSMakeRect(540, y - 1, 160, 26), include_mouse=True)
                doc_points.addSubview_(self._global_fire_key)
            elif key == "scope":
                self._global_scope_key = _key_popup(NSMakeRect(540, y - 1, 160, 26), include_mouse=True)
                doc_points.addSubview_(self._global_scope_key)
            elif key == "backpack":
                self._global_backpack_key = _key_popup(NSMakeRect(540, y - 1, 160, 26), include_mouse=False)
                doc_points.addSubview_(self._global_backpack_key)

        doc_points.addSubview_(
            _label(
                "提示：先点“取点”，再点每行的“填入取点”。坐标是屏幕全局坐标（原点左上，Y 向下；非窗口相对）。"
                " 键：支持 MouseLeft/MouseRight 或键盘按键名（如 E/Space/Tab）。",
                20,
                20,
                820,
            )
        )

        # --------------------
        # Tab 2: 参数
        # --------------------
        doc_params = _add_tab("params", "参数", 860, 440)
        doc_params.addSubview_(_label("手感参数（当前配置档）", 20, 405, 240))

        doc_params.addSubview_(_label("摇杆半径(px)", 20, 365, 90))
        self._joy_radius = NSTextField.alloc().initWithFrame_(NSMakeRect(115, 360, 90, 24))
        doc_params.addSubview_(self._joy_radius)

        doc_params.addSubview_(_label("视角阈值(px)", 235, 365, 80))
        self._cam_thresh = NSTextField.alloc().initWithFrame_(NSMakeRect(315, 360, 70, 24))
        doc_params.addSubview_(self._cam_thresh)

        self._cam_invert = NSButton.alloc().initWithFrame_(NSMakeRect(405, 360, 120, 24))
        self._cam_invert.setButtonType_(NSButtonTypeSwitch)
        self._cam_invert.setTitle_("反转Y")
        doc_params.addSubview_(self._cam_invert)

        doc_params.addSubview_(_label("Tcam(px)", 20, 330, 70))
        self._cam_tcam = NSTextField.alloc().initWithFrame_(NSMakeRect(90, 325, 70, 24))
        doc_params.addSubview_(self._cam_tcam)

        doc_params.addSubview_(_label("视角半径(px)", 180, 330, 90))
        self._cam_radius = NSTextField.alloc().initWithFrame_(NSMakeRect(270, 325, 70, 24))
        doc_params.addSubview_(self._cam_radius)

        doc_params.addSubview_(_label("滚轮映射", 20, 285, 70))
        self._wheel_enabled = NSButton.alloc().initWithFrame_(NSMakeRect(90, 280, 90, 24))
        self._wheel_enabled.setButtonType_(NSButtonTypeSwitch)
        self._wheel_enabled.setTitle_("启用")
        doc_params.addSubview_(self._wheel_enabled)

        doc_params.addSubview_(_label("D", 190, 285, 12))
        self._wheel_d = NSTextField.alloc().initWithFrame_(NSMakeRect(205, 280, 55, 24))
        doc_params.addSubview_(self._wheel_d)

        doc_params.addSubview_(_label("停(ms)", 270, 285, 45))
        self._wheel_stop = NSTextField.alloc().initWithFrame_(NSMakeRect(315, 280, 60, 24))
        doc_params.addSubview_(self._wheel_stop)

        doc_params.addSubview_(_label("锚X", 390, 285, 30))
        self._wheel_anchor_x = NSTextField.alloc().initWithFrame_(NSMakeRect(420, 280, 70, 24))
        doc_params.addSubview_(self._wheel_anchor_x)

        doc_params.addSubview_(_label("Y", 500, 285, 12))
        self._wheel_anchor_y = NSTextField.alloc().initWithFrame_(NSMakeRect(515, 280, 70, 24))
        doc_params.addSubview_(self._wheel_anchor_y)

        doc_params.addSubview_(
            _label("规则：战斗态=以锚点上下拖动；自由鼠标=以鼠标当前位置上下拖动。", 20, 250, 820)
        )

        doc_params.addSubview_(_label("调度参数", 20, 205, 70))
        doc_params.addSubview_(_label("调度Hz", 20, 175, 45))
        self._sched_tick = NSTextField.alloc().initWithFrame_(NSMakeRect(65, 170, 55, 24))
        doc_params.addSubview_(self._sched_tick)

        doc_params.addSubview_(_label("视角Hz", 140, 175, 50))
        self._sched_cam_min = NSTextField.alloc().initWithFrame_(NSMakeRect(190, 170, 55, 24))
        doc_params.addSubview_(self._sched_cam_min)

        doc_params.addSubview_(_label("摇杆Hz", 265, 175, 50))
        self._sched_joy_min = NSTextField.alloc().initWithFrame_(NSMakeRect(315, 170, 55, 24))
        doc_params.addSubview_(self._sched_joy_min)

        doc_params.addSubview_(_label("步长px", 390, 175, 50))
        self._sched_max_step = NSTextField.alloc().initWithFrame_(NSMakeRect(440, 170, 70, 24))
        doc_params.addSubview_(self._sched_max_step)

        # --------------------
        # Tab 3: 自定义
        # --------------------
        doc_custom = _add_tab("custom", "自定义", 860, 420)
        doc_custom.addSubview_(_label("自定义点击（按键→点击）", 20, 385, 240))

        doc_custom.addSubview_(_label("名称", 20, 345, 40))
        self._custom_name = NSTextField.alloc().initWithFrame_(NSMakeRect(60, 340, 220, 24))
        doc_custom.addSubview_(self._custom_name)

        doc_custom.addSubview_(_label("键", 295, 345, 20))
        self._custom_key = _key_popup(NSMakeRect(315, 339, 160, 26), include_mouse=False)
        doc_custom.addSubview_(self._custom_key)
        try:
            self._custom_key.selectItemWithTitle_("E")
        except Exception:
            pass

        btn_add = NSButton.alloc().initWithFrame_(NSMakeRect(480, 339, 120, 26))
        btn_add.setTitle_("添加/替换")
        btn_add.setBezelStyle_(NSBezelStyleRounded)
        btn_add.setTarget_(self)
        btn_add.setAction_("onAddCustom:")
        doc_custom.addSubview_(btn_add)

        doc_custom.addSubview_(_label("X", 20, 305, 12))
        self._custom_x = NSTextField.alloc().initWithFrame_(NSMakeRect(35, 300, 80, 24))
        doc_custom.addSubview_(self._custom_x)
        doc_custom.addSubview_(_label("Y", 125, 305, 12))
        self._custom_y = NSTextField.alloc().initWithFrame_(NSMakeRect(140, 300, 80, 24))
        doc_custom.addSubview_(self._custom_y)

        btn_fill_custom = NSButton.alloc().initWithFrame_(NSMakeRect(235, 299, 100, 26))
        btn_fill_custom.setTitle_("填入取点")
        btn_fill_custom.setBezelStyle_(NSBezelStyleRounded)
        btn_fill_custom.setTarget_(self)
        btn_fill_custom.setAction_("onFillCustomPoint:")
        doc_custom.addSubview_(btn_fill_custom)

        doc_custom.addSubview_(_label("按压(ms)", 350, 305, 55))
        self._custom_hold = NSTextField.alloc().initWithFrame_(NSMakeRect(405, 300, 70, 24))
        doc_custom.addSubview_(self._custom_hold)

        doc_custom.addSubview_(_label("随机(px)", 485, 305, 50))
        self._custom_rrand = NSTextField.alloc().initWithFrame_(NSMakeRect(535, 300, 70, 24))
        doc_custom.addSubview_(self._custom_rrand)

        doc_custom.addSubview_(_label("删除编号(1-based)", 20, 265, 95))
        self._custom_remove_index = NSTextField.alloc().initWithFrame_(NSMakeRect(115, 260, 60, 24))
        doc_custom.addSubview_(self._custom_remove_index)
        btn_remove = NSButton.alloc().initWithFrame_(NSMakeRect(185, 259, 80, 26))
        btn_remove.setTitle_("删除")
        btn_remove.setBezelStyle_(NSBezelStyleRounded)
        btn_remove.setTarget_(self)
        btn_remove.setAction_("onRemoveCustom:")
        doc_custom.addSubview_(btn_remove)

        doc_custom.addSubview_(_label("当前自定义点击：", 20, 235, 120))
        self._custom_list = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 40, 820, 185))
        self._custom_list.setEditable_(False)
        self._custom_list.setBordered_(True)
        self._custom_list.setDrawsBackground_(True)
        self._custom_list.setUsesSingleLineMode_(False)
        self._custom_list.setLineBreakMode_(0)
        self._custom_list.setStringValue_("(空)")
        doc_custom.addSubview_(self._custom_list)

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
        # 固定使用默认路径：设置以 UI 为准并自动保存；不提供“选择配置文件”的入口，避免误操作。
        return self._app.default_config_path()

    @objc.python_method
    def _selected_profile(self) -> Optional[str]:
        # UI 不提供“配置档/Profiles”选择：始终使用第一个 profile
        return None

    @objc.python_method
    def _ensure_log_window(self) -> None:
        if self._log_window is not None:
            return

        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
        w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(0, 0, 820, 460), style, 2, False)
        w.setTitle_("运行日志")
        w.center()

        content = w.contentView()

        # 顶部说明 + 路径
        lbl_path = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 430, 796, 20))
        lbl_path.setEditable_(False)
        lbl_path.setBordered_(False)
        lbl_path.setDrawsBackground_(False)
        try:
            lbl_path.setStringValue_(f"日志文件：{self._app.log_path()}")
        except Exception:
            lbl_path.setStringValue_("日志文件：-")
        content.addSubview_(lbl_path)

        # 文本区（可滚动）
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(12, 52, 796, 370))
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)

        tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 796, 370))
        tv.setEditable_(False)
        tv.setSelectable_(True)
        try:
            tv.setFont_(NSFont.userFixedPitchFontOfSize_(12))
        except Exception:
            pass
        scroll.setDocumentView_(tv)
        content.addSubview_(scroll)

        btn_open = NSButton.alloc().initWithFrame_(NSMakeRect(12, 12, 140, 30))
        btn_open.setTitle_("打开日志文件")
        btn_open.setBezelStyle_(NSBezelStyleRounded)
        btn_open.setTarget_(self)
        btn_open.setAction_("onOpenLogFile:")
        content.addSubview_(btn_open)

        btn_clear = NSButton.alloc().initWithFrame_(NSMakeRect(160, 12, 120, 30))
        btn_clear.setTitle_("清空日志")
        btn_clear.setBezelStyle_(NSBezelStyleRounded)
        btn_clear.setTarget_(self)
        btn_clear.setAction_("onClearLogs:")
        content.addSubview_(btn_clear)

        self._log_window = w
        self._log_text = tv
        self._update_log_window()

    @objc.python_method
    def _update_log_window(self) -> None:
        if self._log_window is None or self._log_text is None:
            return
        try:
            s = self._app.get_recent_logs(400)
        except Exception:
            s = ""
        try:
            self._log_text.setString_(s or "(暂无日志)")
            # 滚动到末尾
            self._log_text.scrollRangeToVisible_((len(s), 0))
        except Exception:
            pass

    @objc.python_method
    def _ensure_overlay(self) -> None:
        if self._overlay is None:
            self._overlay = _MarkerOverlay()
        self._overlay.show()

    @objc.python_method
    def _build_markers(self) -> list[dict]:
        markers: list[dict] = []

        color_map = {
            "joystickCenter": (NSColor.systemGreenColor(), "摇杆中心"),
            # 避免与“运行时点击标记”的蓝/橙混淆
            "cameraAnchor": (NSColor.systemTealColor(), "视角锚点"),
            "fire": (NSColor.systemRedColor(), "开火"),
            "scope": (NSColor.systemPurpleColor(), "开镜"),
            "backpack": (NSColor.systemYellowColor(), "背包"),
        }

        for k, (fx, fy) in self._point_fields.items():
            x = self._safe_float(fx, float("nan"))
            y = self._safe_float(fy, float("nan"))
            if x != x or y != y:  # NaN check
                continue
            color, label = color_map.get(k, (NSColor.systemGrayColor(), k))
            markers.append({"x": x, "y": y, "r": 7.0, "color": color, "label": label})

        # 滚轮锚点（锁定视角/战斗模式下使用）
        if self._wheel_anchor_x is not None and self._wheel_anchor_y is not None:
            x_s = str(self._wheel_anchor_x.stringValue()).strip()
            y_s = str(self._wheel_anchor_y.stringValue()).strip()
            if x_s and y_s:
                try:
                    markers.append(
                        {
                            "x": float(x_s),
                            "y": float(y_s),
                            "r": 7.0,
                            "color": NSColor.systemYellowColor(),
                            "label": "滚轮锚点",
                        }
                    )
                except Exception:
                    pass

        # 自定义点击：从配置里读取（已保存为准）
        if isinstance(self._cfg_dict, dict):
            lst = self._cfg_dict.get("customMappings")
            if isinstance(lst, list):
                for m in lst:
                    if not isinstance(m, dict):
                        continue
                    pt = m.get("point")
                    if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
                        continue
                    try:
                        x = float(pt[0])
                        y = float(pt[1])
                    except Exception:
                        continue
                    key = str(m.get("key") or "").strip()
                    name = str(m.get("name") or "").strip()
                    label = f"{key}:{name}" if key or name else "自定义"
                    markers.append({"x": x, "y": y, "r": 6.0, "color": NSColor.systemTealColor(), "label": label})

        # 运行时：最近一次“实际点击点位”（默认蓝色，按下时橙色）
        try:
            for m in self._app.click_markers():
                try:
                    x = float(m.get("x", 0.0))
                    y = float(m.get("y", 0.0))
                except Exception:
                    continue
                pressed = bool(m.get("pressed"))
                label = str(m.get("label") or "")
                markers.append(
                    {
                        "x": x,
                        "y": y,
                        "r": 6.5,
                        "color": NSColor.systemOrangeColor() if pressed else NSColor.systemBlueColor(),
                        "label": label,
                    }
                )
        except Exception:
            pass

        return markers

    @objc.python_method
    def _update_overlay(self) -> None:
        if self._overlay is None:
            return
        try:
            self._overlay.update(self._build_markers())
        except Exception:
            pass

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
    def _set_key_control(self, ctrl, value: str, default: str) -> None:
        """
        兼容两种 UI 控件：
        - NSPopUpButton：用 selectItemWithTitle_
        - NSTextField：用 setStringValue_
        """
        if ctrl is None:
            return
        v = str(value or "").strip() or str(default)
        # 优先走下拉框
        try:
            ctrl.selectItemWithTitle_(v)
            try:
                if str(ctrl.titleOfSelectedItem() or "") != v:
                    # 配置里可能有旧值：追加到末尾并选中
                    ctrl.addItemWithTitle_(v)
                    ctrl.selectItemWithTitle_(v)
            except Exception:
                pass
            return
        except Exception:
            pass
        # 回退：文本框
        try:
            ctrl.setStringValue_(v)
        except Exception:
            pass

    @objc.python_method
    def _get_key_control(self, ctrl, default: str) -> str:
        if ctrl is None:
            return str(default)
        # 下拉框
        try:
            v = str(ctrl.titleOfSelectedItem() or "").strip()
            return v or str(default)
        except Exception:
            pass
        # 文本框
        try:
            v = str(ctrl.stringValue() or "").strip()
            return v or str(default)
        except Exception:
            return str(default)

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

        # UI 状态（不参与引擎配置解析）：用于记住上次 UI 勾选项/选中的配置档
        ui = self._cfg_dict.get("ui")
        if isinstance(ui, dict):
            try:
                if self._chk_enabled is not None and ui.get("mappingEnabled") is not None:
                    self._chk_enabled.setState_(1 if bool(ui.get("mappingEnabled")) else 0)
                if self._chk_camera is not None and ui.get("cameraLock") is not None:
                    self._chk_camera.setState_(1 if bool(ui.get("cameraLock")) else 0)
                if self._chk_overlay is not None and ui.get("overlayEnabled") is not None:
                    self._chk_overlay.setState_(1 if bool(ui.get("overlayEnabled")) else 0)
            except Exception:
                pass

        g = self._cfg_dict.get("global")
        if not isinstance(g, dict):
            g = {}

        if self._global_enable_hotkey is not None:
            self._set_key_control(self._global_enable_hotkey, str(g.get("enableHotkey") or ""), "F8")
        if self._global_panic_hotkey is not None:
            self._set_key_control(self._global_panic_hotkey, str(g.get("panicHotkey") or ""), "F12")
        if self._global_camera_lock_key is not None:
            self._set_key_control(self._global_camera_lock_key, str(g.get("cameraLockKey") or ""), "Tab")
        if self._global_backpack_key is not None:
            self._set_key_control(self._global_backpack_key, str(g.get("backpackKey") or ""), "B")
        if self._global_move_up_key is not None:
            self._set_key_control(self._global_move_up_key, str(g.get("moveUpKey") or ""), "W")
        if self._global_move_down_key is not None:
            self._set_key_control(self._global_move_down_key, str(g.get("moveDownKey") or ""), "S")
        if self._global_move_left_key is not None:
            self._set_key_control(self._global_move_left_key, str(g.get("moveLeftKey") or ""), "A")
        if self._global_move_right_key is not None:
            self._set_key_control(self._global_move_right_key, str(g.get("moveRightKey") or ""), "D")
        if self._global_fire_key is not None:
            self._set_key_control(self._global_fire_key, str(g.get("fireKey") or ""), "MouseLeft")
        if self._global_scope_key is not None:
            self._set_key_control(self._global_scope_key, str(g.get("scopeKey") or ""), "MouseRight")
        if self._global_rrand_default is not None:
            self._global_rrand_default.setStringValue_(str(g.get("rrandDefaultPx") if g.get("rrandDefaultPx") is not None else 0))

        p = self._profile_dict(self._selected_profile())
        if not isinstance(p, dict):
            return

        points = p.get("points")
        if not isinstance(points, dict):
            points = {}

        legacy_points = {
            "joystickCenter": "C",
            "cameraAnchor": "A",
            "fire": "F",
            "scope": "S",
            "backpack": "I",
        }
        for k, (fx, fy) in self._point_fields.items():
            pt = points.get(k)
            if pt is None:
                old = legacy_points.get(k)
                if old:
                    pt = points.get(old)
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
        if self._cam_thresh is not None:
            # 新字段：thresholdPx；兼容旧字段 sensitivity（映射到阈值倍率）
            thr = camera.get("thresholdPx")
            if thr is None and camera.get("sensitivity") is not None:
                try:
                    thr = float(camera.get("sensitivity")) * 6.0
                except Exception:
                    thr = None
            self._cam_thresh.setStringValue_(str(thr if thr is not None else 6.0))
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
        if self._wheel_anchor_x is not None and self._wheel_anchor_y is not None:
            ap = wheel.get("anchorPoint")
            if ap is None:
                ap = wheel.get("anchor")
            if isinstance(ap, (list, tuple)) and len(ap) == 2:
                self._wheel_anchor_x.setStringValue_(str(ap[0]))
                self._wheel_anchor_y.setStringValue_(str(ap[1]))
            else:
                self._wheel_anchor_x.setStringValue_("")
                self._wheel_anchor_y.setStringValue_("")

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

        tw = self._cfg_dict.get("targetWindow")
        if not isinstance(tw, dict):
            tw = {}
            self._cfg_dict["targetWindow"] = tw
        # 用户诉求：不需要目标窗口检测，因此固定关闭
        tw["enabled"] = False
        tw["pid"] = None
        tw["windowId"] = None
        tw["titleHint"] = str(tw.get("titleHint") or "iPhone Mirroring")

        g = self._cfg_dict.get("global")
        if not isinstance(g, dict):
            g = {}
            self._cfg_dict["global"] = g

        if self._global_enable_hotkey is not None:
            g["enableHotkey"] = self._get_key_control(self._global_enable_hotkey, "F8")
        if self._global_panic_hotkey is not None:
            g["panicHotkey"] = self._get_key_control(self._global_panic_hotkey, "F12")
        if self._global_camera_lock_key is not None:
            g["cameraLockKey"] = self._get_key_control(self._global_camera_lock_key, "Tab")
        if self._global_backpack_key is not None:
            g["backpackKey"] = self._get_key_control(self._global_backpack_key, "B")
        if self._global_move_up_key is not None:
            g["moveUpKey"] = self._get_key_control(self._global_move_up_key, "W")
        if self._global_move_down_key is not None:
            g["moveDownKey"] = self._get_key_control(self._global_move_down_key, "S")
        if self._global_move_left_key is not None:
            g["moveLeftKey"] = self._get_key_control(self._global_move_left_key, "A")
        if self._global_move_right_key is not None:
            g["moveRightKey"] = self._get_key_control(self._global_move_right_key, "D")
        if self._global_fire_key is not None:
            g["fireKey"] = self._get_key_control(self._global_fire_key, "MouseLeft")
        if self._global_scope_key is not None:
            g["scopeKey"] = self._get_key_control(self._global_scope_key, "MouseRight")
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
        # 清理旧命名，避免同时存在两套字段导致混淆
        for old in ("C", "A", "F", "S", "I"):
            points.pop(old, None)

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
        if self._cam_thresh is not None:
            camera["thresholdPx"] = self._safe_float(self._cam_thresh, 6.0)
            # 清理旧字段，避免混淆
            camera.pop("sensitivity", None)
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
        if self._wheel_anchor_x is not None and self._wheel_anchor_y is not None:
            x_s = str(self._wheel_anchor_x.stringValue()).strip()
            y_s = str(self._wheel_anchor_y.stringValue()).strip()
            if x_s and y_s:
                try:
                    wheel["anchorPoint"] = [float(x_s), float(y_s)]
                except Exception:
                    wheel.pop("anchorPoint", None)
            else:
                wheel.pop("anchorPoint", None)
            # 兼容旧字段名
            wheel.pop("anchor", None)

        if self._sched_tick is not None:
            sched["tickHz"] = self._safe_int(self._sched_tick, 120)
        if self._sched_cam_min is not None:
            sched["cameraMinHz"] = self._safe_int(self._sched_cam_min, 50)
        if self._sched_joy_min is not None:
            sched["joystickMinHz"] = self._safe_int(self._sched_joy_min, 20)
        if self._sched_max_step is not None:
            sched["maxStepPx"] = self._safe_float(self._sched_max_step, 6.0)

        # 额外保存 UI 状态（不影响引擎配置解析）
        ui = self._cfg_dict.get("ui")
        if not isinstance(ui, dict):
            ui = {}
            self._cfg_dict["ui"] = ui
        try:
            ui["mappingEnabled"] = bool(self._chk_enabled.state()) if self._chk_enabled is not None else False
            ui["cameraLock"] = bool(self._chk_camera.state()) if self._chk_camera is not None else False
            ui["overlayEnabled"] = bool(self._chk_overlay.state()) if self._chk_overlay is not None else False
        except Exception:
            pass

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
            lines.append(f"{i}. {name} | 按键={key} | 坐标={pt} | 按压={hold}ms | 随机={rrand}")
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

    def onShowLogs_(self, _sender) -> None:
        try:
            self._ensure_log_window()
            self._log_window.makeKeyAndOrderFront_(None)
        except Exception as e:
            self._alert("打开日志失败", str(e))

    def onOpenLogFile_(self, _sender) -> None:
        try:
            self._app.open_in_editor(self._app.log_path())
        except Exception as e:
            self._alert("打开失败", str(e))

    def onClearLogs_(self, _sender) -> None:
        try:
            self._app.clear_logs()
            self._update_log_window()
        except Exception as e:
            self._alert("清空失败", str(e))

    def onToggleOverlay_(self, _sender) -> None:
        enabled = bool(self._chk_overlay.state()) if self._chk_overlay is not None else False
        try:
            if enabled:
                self._ensure_overlay()
                self._update_overlay()
            else:
                if self._overlay is not None:
                    self._overlay.hide()
                self._overlay = None
        except Exception as e:
            self._alert("设置失败", str(e))

    def onDetectFrontmost_(self, _sender) -> None:
        try:
            from mirroring_keymap.macos.window import get_frontmost_debug

            info = get_frontmost_debug(max_windows=10)
            pid = int(info.get("pid", -1))
            name = str(info.get("name") or "")
            wins = info.get("windows") if isinstance(info.get("windows"), list) else []
            wins_s = "\n".join([f"- {w}" for w in wins]) if wins else "(无窗口标题/无法获取)"

            self._alert("当前前台信息", f"应用：{name}\nPID：{pid}\n窗口标题：\n{wins_s}\n\n已填入到“目标窗口/PID”，请点击“保存配置”。")

            if self._tw_pid is not None and pid > 0:
                self._tw_pid.setStringValue_(str(pid))
            if self._tw_title_hint is not None and wins:
                self._tw_title_hint.setStringValue_(str(wins[0]))
        except Exception as e:
            self._alert("检测失败", str(e))

    def onStart_(self, _sender) -> None:
        # 避免焦点停留在某个输入框导致按键被“打字进去”
        try:
            if self._window is not None:
                self._window.makeFirstResponder_(None)
        except Exception:
            pass

        # 启动前自动保存一次，确保“直接点开始”也能使用当前表单值
        if not self._save_current_config():
            return
        try:
            self._app.start(self._cfg_path(), self._selected_profile())
            # “启用映射/视角锁定”以 UI 勾选项为准（避免启动后 WASD 无反应）
            try:
                if self._chk_enabled is not None:
                    self._app.set_mapping_enabled(bool(self._chk_enabled.state()))
                if self._chk_camera is not None:
                    self._app.set_camera_lock(bool(self._chk_camera.state()))
            except Exception:
                pass
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

        try:
            self._cfg_mtime = Path(path).expanduser().stat().st_mtime
        except Exception:
            self._cfg_mtime = None

        # UI 固定使用 profiles[0]，这里确保 profiles 至少有 1 个
        try:
            if isinstance(self._cfg_dict, dict):
                profiles = self._cfg_dict.get("profiles")
                if not isinstance(profiles, list) or not profiles:
                    self._cfg_dict["profiles"] = [{"name": "默认", "points": {}}]
                else:
                    p0 = profiles[0]
                    if isinstance(p0, dict):
                        p0["name"] = str(p0.get("name") or "默认")
        except Exception:
            pass

        # 同步表单
        self._sync_ui_from_cfg()

        # 尝试严格校验（不阻塞编辑）
        try:
            self._app.load_config(path)
        except Exception as e:
            self._log.debug("config validate failed: %s", e)

    @objc.python_method
    def _save_current_config(self) -> bool:
        """
        将 UI 表单写入配置文件并做严格校验。
        返回 True 表示保存成功且配置可被引擎加载。
        """
        if not isinstance(self._cfg_dict, dict):
            self.onReloadConfig_(None)
        if not isinstance(self._cfg_dict, dict):
            return False

        path = self._cfg_path()
        try:
            self._apply_ui_to_cfg()
            # 关键校验：锁定键与背包键不能相同，否则会出现“按一次同时触发两种行为”的混乱。
            try:
                g = self._cfg_dict.get("global") if isinstance(self._cfg_dict, dict) else None
                if isinstance(g, dict):
                    cam_k = str(g.get("cameraLockKey") or "").strip()
                    bag_k = str(g.get("backpackKey") or "").strip()
                    if cam_k and bag_k and cam_k == bag_k:
                        self._alert("保存失败", "“视角锁定键”和“背包键”不能设置为同一个键，请修改其中一个。")
                        return False
            except Exception:
                pass
            self._app.save_config_dict(path, self._cfg_dict)
        except Exception as e:
            self._alert("保存失败", str(e))
            return False

        # 保存后做一次严格校验，提示更明确
        try:
            self._app.load_config(path)
        except Exception as e:
            self._alert("保存成功但配置无效", str(e))
            return False

        try:
            self._cfg_mtime = Path(path).expanduser().stat().st_mtime
        except Exception:
            self._cfg_mtime = None

        return True

    def onSaveConfig_(self, _sender) -> None:
        try:
            if self._window is not None:
                self._window.makeFirstResponder_(None)
        except Exception:
            pass

        snap = self._app.snapshot()
        was_running = bool(snap.get("running"))
        prev_mapping = bool(snap.get("mapping_enabled"))
        prev_camera = bool(snap.get("camera_lock"))

        if not self._save_current_config():
            return

        # 运行中保存：自动重启引擎使配置立即生效（解决“改了坐标但没变化”）
        if was_running:
            try:
                self._app.start(self._cfg_path(), self._selected_profile())
                self._app.set_mapping_enabled(prev_mapping)
                self._app.set_camera_lock(prev_camera)
            except Exception as e:
                self._alert("应用配置失败", str(e))

        self.onReloadConfig_(None)

    def onFillPoint_(self, sender) -> None:
        if self._last_pick is None:
            self._alert("无法填入", "请先点击“取点（点击）”获取坐标。")
            return
        tag = int(sender.tag())
        keys = ["joystickCenter", "cameraAnchor", "fire", "scope", "backpack"]
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

    def onFillCustomPoint_(self, _sender) -> None:
        if self._last_pick is None:
            self._alert("无法填入", "请先点击“取点（点击）”获取坐标。")
            return
        if self._custom_x is None or self._custom_y is None:
            return
        x, y = self._last_pick
        self._custom_x.setStringValue_(f"{x:.1f}")
        self._custom_y.setStringValue_(f"{y:.1f}")

    def onAddCustom_(self, _sender) -> None:
        if not isinstance(self._cfg_dict, dict):
            self.onReloadConfig_(None)
        if not isinstance(self._cfg_dict, dict):
            return

        name = str(self._custom_name.stringValue() if self._custom_name else "").strip() or "Custom"
        key = self._normalize_key(self._get_key_control(self._custom_key, "E"))
        if not key:
            self._alert("新增失败", "请选择触发键（例如：E / R / Space / 1）。")
            return

        x_s = str(self._custom_x.stringValue() if self._custom_x else "").strip()
        y_s = str(self._custom_y.stringValue() if self._custom_y else "").strip()
        x = float(x_s) if x_s else None
        y = float(y_s) if y_s else None
        if x is None or y is None:
            # 兼容：用户先点击“取点”，坐标会复制到剪贴板（JSON），这里可直接读取
            try:
                pb = NSPasteboard.generalPasteboard()
                s = pb.stringForType_(NSPasteboardTypeString)
                if s:
                    d = json.loads(str(s))
                    if isinstance(d, dict):
                        if x is None and "x" in d:
                            x = float(d["x"])
                        if y is None and "y" in d:
                            y = float(d["y"])
            except Exception:
                pass
        if x is None or y is None:
            self._alert("新增失败", "请填写 X/Y，或先取点（坐标会在剪贴板中）再添加。")
            return

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
            self._lbl_status.setStringValue_(
                "状态：未启动\n"
                "提示：首次运行请在系统设置 → 隐私与安全性中开启“输入监控”和“辅助功能”权限。"
            )
            self._btn_start.setEnabled_(True)
            self._btn_stop.setEnabled_(False)
            return

        self._btn_start.setEnabled_(False)
        self._btn_stop.setEnabled_(True)

        def yn(v: object) -> str:
            return "是" if bool(v) else "否"

        mode_map = {"paused": "暂停", "battle": "战斗", "free": "自由鼠标"}
        mode_cn = mode_map.get(str(snap.get("mode") or ""), str(snap.get("mode") or ""))

        # 诊断：WASD 是否被捕获（EventTap/轮询）
        def bit4(d: object) -> str:
            if not isinstance(d, dict):
                return "----"
            try:
                u = "1" if int(d.get("up") or 0) else "0"
                l = "1" if int(d.get("left") or 0) else "0"
                dn = "1" if int(d.get("down") or 0) else "0"
                r = "1" if int(d.get("right") or 0) else "0"
                return f"{u}{l}{dn}{r}"
            except Exception:
                return "----"

        et_bits = bit4(snap.get("move_eventtap"))
        pol_bits = bit4(snap.get("move_polled"))
        poll_ok = yn(snap.get("poll_move_ok"))
        secure = snap.get("secure_input")
        secure_txt = "未知" if secure is None else yn(bool(secure))
        age = snap.get("last_kbd_event_age_ms")
        age_txt = "-" if age is None else f"{int(age)}ms"

        hint = ""
        if snap.get("accessibility_trusted") is False:
            hint = "提示：未授予“辅助功能”权限，可能无法注入点击/拖动。请到 系统设置 → 隐私与安全性 → 辅助功能 授权。"
        elif secure is True:
            hint = (
                "提示：检测到系统已开启“安全输入”，第三方程序无法读取键盘事件，WASD 将无法工作。"
                "请关闭可能开启安全输入的程序/输入框后重试。"
            )
        elif not bool(snap.get("target_active")):
            hint = "提示：为避免误触，当前前台为本程序时会自动暂停映射。请切换到 iPhone Mirroring/游戏窗口后再测试按键。"
        elif not bool(snap.get("mapping_enabled")):
            hint = "提示：映射未启用，请勾选“启用映射”或按启用热键（默认 F8）。"
        elif str(snap.get("mode")) != "battle":
            hint = "提示：当前为自由鼠标模式（WASD 摇杆/开火/开镜/自定义点击都不会生效），请开启“视角锁定”（默认 Tab）。"

        txt = (
            f"状态：运行中 | 模式：{mode_cn} | 映射：{yn(snap.get('mapping_enabled'))} | 视角锁定：{yn(snap.get('camera_lock'))} | 背包：{yn(snap.get('backpack_open'))}\n"
            f"WASD检测(上左下右)：ET={et_bits} | 轮询={pol_bits} | 轮询可用：{poll_ok} | 安全输入：{secure_txt} | 最近键盘事件：{age_txt}\n"
            f"{hint}"
        )
        self._lbl_status.setStringValue_(txt)

        # 同步复选框（避免 UI 和热键状态不一致）
        try:
            self._chk_enabled.setState_(1 if snap.get("mapping_enabled") else 0)
            self._chk_camera.setState_(1 if snap.get("camera_lock") else 0)
        except Exception:
            pass

        # 更新日志窗口与覆盖层
        try:
            self._update_log_window()
        except Exception:
            pass
        try:
            if self._overlay is not None:
                self._update_overlay()
        except Exception:
            pass
