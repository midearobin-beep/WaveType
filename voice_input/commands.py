"""语音命令（P2，对标 Typeless VOICE_COMMAND 模式）。

整句精确命中命令词时直接发按键，**不进 LLM、不走润色**：
- "换行" → Return；"撤销" → Cmd+Z；"全选/复制/粘贴/保存" → 对应快捷键
- 只认整句匹配（去口癖和标点后等于命令词，或同一命令词重复 N 次），
  口述正文里提到"保存"二字绝不会误触发。
- 命中与否都必须留日志：命令误吞口述是这类功能最隐蔽的故障。
"""
from __future__ import annotations

import logging

import Quartz

from .polish import _FILLERS

log = logging.getLogger(__name__)

_CMD = Quartz.kCGEventFlagMaskCommand
_SHIFT = Quartz.kCGEventFlagMaskShift

# keycode 参考 Quartz Events.h：Return 0x24, Delete 0x33, Tab 0x30, Space 0x31,
# A 0x00, S 0x01, X 0x07, C 0x08, V 0x09, Z 0x06
_ACTIONS: dict[str, list[tuple[int, int]]] = {
    "换行": [(0x24, 0)], "回车": [(0x24, 0)], "下一行": [(0x24, 0)], "新行": [(0x24, 0)],
    "空格": [(0x31, 0)],
    "制表符": [(0x30, 0)], "缩进": [(0x30, 0)],
    "退格": [(0x33, 0)], "删掉": [(0x33, 0)],
    "撤销": [(0x06, _CMD)], "重做": [(0x06, _CMD | _SHIFT)],
    "全选": [(0x00, _CMD)], "复制": [(0x08, _CMD)], "粘贴": [(0x09, _CMD)],
    "剪切": [(0x07, _CMD)], "保存": [(0x01, _CMD)],
}

_STRIP = "，。、！？；：,.!?;:…—－- \t\n\r"


def _normalize(text: str) -> str:
    """去口癖和标点。命令判定的输入净化，"嗯换行吧" → "换行"。"""
    t = text.strip()
    for f in _FILLERS:
        t = t.replace(f, "")
    return t.strip(_STRIP).replace("吧", "").replace("一下", "")


def match_command(text: str) -> tuple[str, int] | None:
    """整句命中命令 → (命令词, 重复次数)；否则 None。

    支持同一命令词连续重复（"换行换行" → Return×2）。
    """
    t = _normalize(text)
    if not t or len(t) > 12:
        return None
    if t in _ACTIONS:
        return t, 1
    for word in sorted(_ACTIONS, key=len, reverse=True):
        if len(t) % len(word) == 0 and t == word * (len(t) // len(word)):
            return word, len(t) // len(word)
    return None


def _press(keycode: int, flags: int) -> None:
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(src, keycode, down)
        if flags:
            Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def execute(word: str, times: int = 1) -> None:
    """执行命令词对应的按键序列。"""
    actions = _ACTIONS.get(word)
    if not actions:
        return
    for _ in range(max(1, min(times, 5))):  # 重复上限 5，防异常放大
        for keycode, flags in actions:
            _press(keycode, flags)
    log.info("语音命令执行：%s ×%d", word, times)
