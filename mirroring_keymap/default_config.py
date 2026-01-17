from __future__ import annotations

# 注意：该默认配置用于“首次启动 UI 时自动生成”，避免 .app 内默认路径找不到 config.json 导致启动失败。
# 用户应通过 UI 的“打开”按钮编辑该文件，填入实际点位坐标。

DEFAULT_CONFIG_JSON = """\
{
  "version": 1,
  "targetWindow": {
    "titleHint": "iPhone Mirroring",
    "enabled": false,
    "pid": null,
    "windowId": null
  },
  "global": {
    "enableHotkey": "F8",
    "panicHotkey": "F12",
    "cameraLockKey": "CapsLock",
    "backpackKey": "Tab",
    "moveUpKey": "W",
    "moveDownKey": "S",
    "moveLeftKey": "A",
    "moveRightKey": "D",
    "fireKey": "MouseLeft",
    "scopeKey": "MouseRight",
    "rrandDefaultPx": 0
  },
  "profiles": [
    {
      "name": "默认",
      "points": {
        "joystickCenter": [200, 200],
        "cameraAnchor": [800, 400],
        "fire": [950, 260],
        "scope": [1010, 260],
        "backpack": [980, 680]
      },
      "joystick": { "radiusPx": 120, "tauMs": 60, "rrandPx": null },
      "camera": {
        "tcamPx": 3,
        "radiusPx": 80,
        "invertY": false,
        "thresholdPx": 10.0,
        "rrandPx": null
      },
      "fire": { "mode": "tap", "tapHoldMs": 30, "rrandPx": null },
      "scope": { "mode": "tap", "tapHoldMs": 30, "rrandPx": null },
      "wheel": { "enabled": true, "dPx": 8, "stopMs": 120, "invert": false, "anchorPoint": [800, 400], "rrandPx": null },
      "scheduler": {
        "tickHz": 120,
        "cameraMinHz": 50,
        "joystickMinHz": 20,
        "cameraBudgetMs": 6,
        "maxStepPx": 6
      }
    }
  ],
  "customMappings": [
    { "name": "交互/拾取", "key": "E", "type": "tap", "point": [820, 300], "tapHoldMs": 30, "rrandPx": 2 }
  ]
}
"""
