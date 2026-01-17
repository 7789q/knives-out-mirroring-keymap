# 荒野行动 iPhone Mirroring 单指按键映射（MVP）

本仓库实现一个运行在 macOS 上的“键鼠 → 单指触控等效”工具，用于 iPhone Mirroring 场景下的 1:1 输入映射与时间片调度（不包含自动瞄准/压枪等非公平功能）。

PRD：`iphone-mirroring-keymap-prd.md`

## 安装（开发环境）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

## 权限（必须）

首次运行前请在系统设置中为终端/运行进程开启：

- 隐私与安全性 → **输入监控（Input Monitoring）**
- 隐私与安全性 → **辅助功能（Accessibility）**

否则全局事件捕获/注入会失败（或创建 EventTap 失败）。

## 快速开始

1) 复制并编辑示例配置：

```bash
cp config.example.json config.json
```

2) 干跑（只解析配置，不做任何捕获/注入）：

```bash
mirroring-keymap --config config.json --dry-run
```

3) 取点（点击一次屏幕，打印坐标；用于填配置里的摇杆中心/视角锚点/开火/开镜/背包按钮等点位）：

```bash
mirroring-keymap pick
```

4) 正式运行：

```bash
mirroring-keymap --config config.json --run
```

## UI（macOS）

开发运行（不打包）：

```bash
source .venv/bin/activate
python -m mirroring_keymap.ui_main
```

打包生成可双击启动的 `.app`：

```bash
./scripts/build_app.sh
open dist/MirroringKeymap.app
```

首次启动 UI 时，会在 `~/Library/Application Support/MirroringKeymap/config.json` 自动生成一份默认配置（点位为占位值），你可以在 UI 里点“打开”直接编辑。

UI 支持直接修改常用参数并保存（配置档点位/部分手感参数/全局热键），也支持在“自定义点击”里新增多条 `按键 -> 点击坐标` 映射。

## 坐标说明

当前实现使用 Quartz 全局坐标系（与 `CGEventGetLocation`/`CGWarpMouseCursorPosition` 一致，通常 **原点在主屏左下角**，单位为 points）。建议用 `mirroring-keymap pick` 取点，避免手工换算。

## 默认热键（可在配置中修改）

- 启用/禁用映射：`F8`
- 紧急停止：`F12`（立即抬起所有按住并恢复光标）
- 视角锁定：`CapsLock`（战斗/自由鼠标切换）
- 背包：`Tab`（打开进入自由鼠标；关闭自动回战斗）
