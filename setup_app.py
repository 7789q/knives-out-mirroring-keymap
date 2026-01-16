from __future__ import annotations

from setuptools import setup

APP = ["mirroring_keymap/ui_main.py"]

OPTIONS = {
    "argv_emulation": True,
    "packages": ["mirroring_keymap"],
    "plist": {
        "CFBundleName": "MirroringKeymap",
        "CFBundleDisplayName": "MirroringKeymap",
        "CFBundleIdentifier": "com.example.mirroringkeymap",
        "CFBundleShortVersionString": "0.1.0",
        "NSHumanReadableCopyright": "Copyright (c) 2026",
    },
}

setup(
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)

