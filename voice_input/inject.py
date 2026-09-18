"""CGEvent 逐字注入：模拟真实键盘 Unicode 事件（Typeless 手感）。

不走剪贴板、不需要 Cmd+V、不经过输入法，中英文混排直接上屏。
CGEventKeyboardSetUnicodeString 单次最多 20 个 UTF-16 code unit，
按 code unit 分块并避开代理对（emoji 等 BMP 外字符）被拆散。
"""
from __future__ import annotations

import logging
import time

import Quartz

log = logging.getLogger(__name__)

_MAX_UNICHARS = 20


def _chunks(text: str, max_units: int = _MAX_UNICHARS) -> list[str]:
    """按 UTF-16 code unit 数切块（单次注入上限 20 units），emoji 等多单元字符不拆散。"""
    out, buf, units = [], [], 0
    for ch in text:
        n = len(ch.encode("utf-16-le")) // 2
        if units + n > max_units:
            out.append("".join(buf))
            buf, units = [], 0
        buf.append(ch)
        units += n
    if buf:
        out.append("".join(buf))
    return out


def _post_text(s: str) -> None:
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(src, 0, down)
        Quartz.CGEventKeyboardSetUnicodeString(ev, len(s), s)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def type_text(text: str, chars_per_second: float = 40.0, initial_delay_ms: int = 50) -> None:
    """把 text 逐字"打"进当前焦点输入框。"""
    if not text:
        return
    time.sleep(initial_delay_ms / 1000.0)
    blocks = _chunks(text)
    interval = _MAX_UNICHARS / max(chars_per_second, 1.0)
    t0 = time.monotonic()
    for i, block in enumerate(blocks):
        _post_text(block)
        # 匀速节流：按字符数对齐目标速度，避免长文本越打越快
        target = t0 + (i + 1) * interval
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    log.info("注入完成: %d 字 (%.1fs)", len(text), time.monotonic() - t0)


if __name__ == "__main__":
    # 手动测试：python -m voice_input.inject
    print("3 秒后向当前焦点输入框打字…")
    time.sleep(3)
    type_text("测试 injection：voice input 逐字上屏。Hello, mixed 中英混排 ✅")
