"""菜单栏图标（NSStatusBar）：运行状态 + 快捷入口。

- 常驻菜单栏语音气泡图标，template 渲染，深浅色自适应
- 录音时图标旁显示单色圆点；菜单中显示模式名（听写/翻译/问答）
- 菜单：状态 / 词典与风格画像统计 / 打开配置文件 / 退出

必须在主线程创建（NSApplication 就绪后）；所有更新经 AppHelper.callAfter
marshal 回主线程，状态更新方法是纯 Python 方法即可被 callAfter 调用。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_MODE_LABEL = {"dictate": "听写中", "translate": "翻译中", "ask": "问答中"}
_LAUNCHD_LABEL = "com.robin.wavetype"


def _launchd_target() -> str | None:
    """在 launchd 下运行时返回 gui/<uid>/<label>，否则 None。"""
    if os.environ.get("XPC_SERVICE_NAME") == _LAUNCHD_LABEL:
        return f"gui/{os.getuid()}/{_LAUNCHD_LABEL}"
    return None


def _terminate() -> None:
    from AppKit import NSApplication
    NSApplication.sharedApplication().terminate_(None)


def _quit() -> None:
    """退出。launchd 下 KeepAlive=true，直接退出会被立刻拉起，
    所以要 bootout 注销服务（下次登录自动恢复）。"""
    target = _launchd_target()
    if target is None:
        _terminate()
        return
    log.info("菜单退出：launchctl bootout %s", target)
    subprocess.Popen(["/bin/launchctl", "bootout", target])


def _restart() -> None:
    target = _launchd_target()
    if target is None:
        log.info("菜单重启：execv")
        os.execv(sys.executable, [sys.executable, *sys.argv])
    log.info("菜单重启：launchctl kickstart -k %s", target)
    subprocess.Popen(["/bin/launchctl", "kickstart", "-k", target])


def _make_voice_image(size: int = 18):
    """语音波形置于对话气泡中；使用 template 跟随系统菜单栏颜色。"""
    from AppKit import NSBezierPath, NSColor, NSImage
    from Foundation import NSPoint, NSSize

    image = NSImage.alloc().initWithSize_(NSSize(size, size))
    image.lockFocus()
    try:
        scale = size / 18.0
        point = lambda x, y: NSPoint(x * scale, y * scale)
        NSColor.blackColor().setStroke()

        # 圆角对话气泡及一笔连成的尾巴，在 18 pt 下保留足够的留白。
        path = NSBezierPath.bezierPath()
        path.setLineWidth_(1.45 * scale)
        path.setLineCapStyle_(1)
        path.setLineJoinStyle_(1)
        path.moveToPoint_(point(5.8, 4.2))
        path.lineToPoint_(point(3.3, 2.3))
        path.lineToPoint_(point(3.3, 4.4))
        path.curveToPoint_controlPoint1_controlPoint2_(
            point(1.9, 6.2), point(2.4, 4.6), point(1.9, 5.2))
        path.lineToPoint_(point(1.9, 12.5))
        path.curveToPoint_controlPoint1_controlPoint2_(
            point(4.4, 15.0), point(1.9, 13.9), point(3.0, 15.0))
        path.lineToPoint_(point(13.6, 15.0))
        path.curveToPoint_controlPoint1_controlPoint2_(
            point(16.1, 12.5), point(15.0, 15.0), point(16.1, 13.9))
        path.lineToPoint_(point(16.1, 6.7))
        path.curveToPoint_controlPoint1_controlPoint2_(
            point(13.6, 4.2), point(16.1, 5.3), point(15.0, 4.2))
        path.closePath()
        path.stroke()

        # 三个圆头竖线既是声音波形，也暗示语音转为文字。
        wave = NSBezierPath.bezierPath()
        wave.setLineWidth_(1.55 * scale)
        wave.setLineCapStyle_(1)
        for x, bottom, top in ((6.0, 8.1, 10.4), (9.0, 6.8, 11.7),
                               (12.0, 8.1, 10.4)):
            wave.moveToPoint_(point(x, bottom))
            wave.lineToPoint_(point(x, top))
        wave.stroke()
    finally:
        image.unlockFocus()
    image.setTemplate_(True)
    return image


class StatusBarController:
    def __init__(self, config_path: Path) -> None:
        from AppKit import (NSMenu, NSMenuItem, NSStatusBar,
                            NSVariableStatusItemLength)

        self._config_path = Path(config_path)
        self._recording = False
        self._mode = "dictate"

        self._item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)
        button = self._item.button()
        button.setImage_(_make_voice_image())
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

        restart_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "重启 WaveType", "restartApp:", "")
        restart_item.setTarget_(self._target())
        self._menu.addItem_(restart_item)

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

                def restartApp_(self, _sender):
                    _restart()

                def quitApp_(self, _sender):
                    _quit()

            self._target_obj = _Target.alloc().init()
        return self._target_obj

    # ---- 状态更新（经 AppHelper.callAfter 在主线程调用） ----

    def set_recording(self, recording: bool, mode: str = "dictate") -> None:
        self._recording = recording
        self._mode = mode
        label = _MODE_LABEL.get(mode, "听写中") if recording else "空闲"
        dot = " •" if recording else ""
        self._status_item.setTitle_(f"状态：{label}")
        button = self._item.button()
        if button is not None:
            button.setTitle_(dot)

    def set_stats(self, dict_count: int, style_on: bool) -> None:
        profile = " · 风格画像已启用" if style_on else ""
        self._stats_item.setTitle_(f"词典：{dict_count} 条{profile}")
