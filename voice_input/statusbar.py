"""菜单栏图标（NSStatusBar）：运行状态 + 快捷入口。

- 常驻菜单栏麦克风图标（assets/icon.png，template 渲染，深浅色自适应）
- 录音时图标旁显示红点 + 模式名（听写/翻译/问答）
- 菜单：状态 / 词典与风格画像统计 / 打开配置文件 / 退出

必须在主线程创建（NSApplication 就绪后）；所有更新经 AppHelper.callAfter
marshal 回主线程，状态更新方法是纯 Python 方法即可被 callAfter 调用。
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_ICON = Path(__file__).resolve().parent.parent / "assets" / "icon.png"

_MODE_LABEL = {"dictate": "听写中", "translate": "翻译中", "ask": "问答中"}


class StatusBarController:
    def __init__(self, config_path: Path) -> None:
        from AppKit import (NSImage, NSMenu, NSMenuItem, NSStatusBar,
                            NSVariableStatusItemLength)

        self._config_path = Path(config_path)
        self._recording = False
        self._mode = "dictate"

        self._item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)
        button = self._item.button()
        if _ICON.is_file():
            image = NSImage.alloc().initWithContentsOfFile_(str(_ICON))
            if image is not None:
                from Foundation import NSSize
                image.setSize_(NSSize(18, 18))
                image.setTemplate_(True)  # 模板渲染：深浅色菜单栏都清晰
                button.setImage_(image)
        if button.image() is None:
            button.setTitle_("🎙")  # 图标文件缺失时的兜底
        button.setToolTip_("WaveType 语音输入")

        self._menu = NSMenu.alloc().init()
        self._status_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "状态：空闲", None, "")
        self._status_item.setEnabled_(False)
        self._menu.addItem_(self._status_item)

        self._stats_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "词典：- 条", None, "")
        self._stats_item.setEnabled_(False)
        self._menu.addItem_(self._stats_item)

        self._menu.addItem_(NSMenuItem.separatorItem())

        open_cfg = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "打开配置文件", "openConfig:", "")
        open_cfg.setTarget_(self._target())
        self._menu.addItem_(open_cfg)

        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "退出 WaveType", "quitApp:", "q")
        quit_item.setTarget_(self._target())
        self._menu.addItem_(quit_item)

        self._item.setMenu_(self._menu)
        log.info("菜单栏图标已创建")

    # 菜单 action 需要一个 NSObject 目标；惰性创建一个，挂在本对象上
    def _target(self):
        if getattr(self, "_target_obj", None) is None:
            from Foundation import NSObject

            controller = self

            class _Target(NSObject):
                def openConfig_(self, _sender):
                    from AppKit import NSWorkspace
                    NSWorkspace.sharedWorkspace().openFile_(str(controller._config_path))

                def quitApp_(self, _sender):
                    from AppKit import NSApplication
                    NSApplication.sharedApplication().terminate_(None)

            self._target_obj = _Target.alloc().init()
        return self._target_obj

    # ---- 状态更新（经 AppHelper.callAfter 在主线程调用） ----

    def set_recording(self, recording: bool, mode: str = "dictate") -> None:
        self._recording = recording
        self._mode = mode
        label = _MODE_LABEL.get(mode, "听写中") if recording else "空闲"
        dot = "🔴 " if recording else ""
        self._status_item.setTitle_(f"状态：{label}")
        button = self._item.button()
        if button is not None:
            button.setTitle_(dot)  # 有图标时红点叠在图标旁；无图标时替代 🎙

    def set_stats(self, dict_count: int, style_on: bool) -> None:
        profile = " · 风格画像已启用" if style_on else ""
        self._stats_item.setTitle_(f"词典：{dict_count} 条{profile}")
