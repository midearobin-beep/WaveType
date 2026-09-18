"""选区文本读取：Accessibility API 优先，剪贴板兜底（用完恢复原内容）。

用于纠错捕获：用户手动改正后选中正确的词/句，Ctrl+Fn 触发读取。
"""
from __future__ import annotations

import logging
import time

import Quartz
from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    AXUIElementCreateSystemWide,
    kAXFocusedUIElementAttribute,
    kAXSelectedTextAttribute,
)

log = logging.getLogger(__name__)


def _read_ax() -> str | None:
    system = AXUIElementCreateSystemWide()
    err, focused = AXUIElementCopyAttributeValue(system, kAXFocusedUIElementAttribute, None)
    if err != 0 or focused is None:
        return None
    err, sel = AXUIElementCopyAttributeValue(focused, kAXSelectedTextAttribute, None)
    if err != 0 or not sel:
        return None
    return str(sel)


def _read_clipboard() -> str | None:
    """模拟 Cmd+C 抢一次剪贴板，读完恢复原内容。"""
    from AppKit import NSPasteboard, NSStringPboardType

    pb = NSPasteboard.generalPasteboard()
    old = pb.stringForType_("public.plain-text")
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    # Cmd down → C down → C up → Cmd up
    for key, down, flags in ((0x37, True, 0), (0x08, True, Quartz.kCGEventFlagMaskCommand),
                             (0x08, False, Quartz.kCGEventFlagMaskCommand), (0x37, False, 0)):
        ev = Quartz.CGEventCreateKeyboardEvent(src, key, down)
        if flags:
            Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    time.sleep(0.15)  # 等目标 App 把选区写进剪贴板
    new = pb.stringForType_("public.plain-text")
    # 恢复原剪贴板
    if old is not None:
        pb.clearContents()
        pb.setString_forType_(old, "public.plain-text")
    if new and new != old:
        return str(new)
    return None


def read_selected_text() -> str | None:
    """读当前选区文本；无选区或失败返回 None。"""
    text = _read_ax()
    if text:
        log.info("选区(AX): %s", text[:50])
        return text
    text = _read_clipboard()
    if text:
        log.info("选区(剪贴板): %s", text[:50])
        return text
    log.info("未读到选区")
    return None
