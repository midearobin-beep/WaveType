"""全局热键：Quartz event tap 监听 Fn 键（flagsChanged）。

Fn 在 macOS 上不是普通按键，走 flagsChanged 事件，
标志位 kCGEventFlagMaskSecondaryFn (0x800000) 表示按下状态。

两种模式：
- hold  ：按住说话、松开上屏（Typeless 手感）
- toggle：按一下开始、再按一下结束

注意：系统设置 → 键盘 →「按下 Fn 键时」需设为「什么都不做」，
否则 Fn 会被系统听写/输入法切换抢走。
"""
from __future__ import annotations

import logging
from collections.abc import Callable

import Quartz

log = logging.getLogger(__name__)

_FN_FLAG = 0x800000  # kCGEventFlagMaskSecondaryFn
_CTRL_FLAG = 0x40000  # kCGEventFlagMaskControl


class FnHotkey:
    """Fn hold-to-talk + Ctrl+Fn 点按纠错捕获。

    - 按住 Fn（hold 模式）：说话，松开上屏
    - 按住 Ctrl 再点按 Fn：不录音，触发 on_correct（读选区 → 学纠错对）
    """

    def __init__(self, on_press: Callable[[], None], on_release: Callable[[], None],
                 mode: str = "hold", on_correct: Callable[[], None] | None = None) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._on_correct = on_correct
        self._mode = mode
        self._down = False
        self._active = False  # toggle 模式下的录音状态
        self._correcting = False  # 本次按下是纠错手势
        self._loop = None

    def _callback(self, proxy, event_type, event, refcon):
        if event_type == Quartz.kCGEventTapDisabledByTimeout:
            log.warning("event tap 超时被系统禁用，重新启用")
            Quartz.CGEventTapEnable(self._tap, True)
            return event
        if event_type == Quartz.kCGEventFlagsChanged:
            flags = Quartz.CGEventGetFlags(event)
            down = bool(flags & _FN_FLAG)
            log.debug("flagsChanged flags=%#x down=%s", flags, down)
            if down != self._down:
                self._down = down
                self._dispatch(down, ctrl=bool(flags & _CTRL_FLAG))
        return event

    def _dispatch(self, down: bool, ctrl: bool = False) -> None:
        try:
            if down and ctrl and self._on_correct is not None:
                self._correcting = True
                self._on_correct()
                return
            if self._mode == "hold":
                if down:
                    self._on_press()
                elif not self._correcting:
                    self._on_release()
                self._correcting = False
            else:  # toggle：只在按下沿翻转
                if down:
                    self._active = not self._active
                    (self._on_press if self._active else self._on_release)()
        except Exception:
            log.exception("热键回调异常")

    def run(self) -> None:
        """在当前线程跑 event tap（需要 CFRunLoop，建议放主线程）。"""
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged),
            self._callback,
            None,
        )
        if self._tap is None:
            raise RuntimeError(
                "CGEventTapCreate 失败：请在 系统设置 → 隐私与安全性 → "
                "输入监控 中授权当前终端/应用"
            )
        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        loop = Quartz.CFRunLoopGetCurrent()
        self._loop = loop
        Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        log.info("Fn 热键监听已启动（mode=%s）", self._mode)
        Quartz.CFRunLoopRun()

    def stop(self) -> None:
        if self._loop is not None:
            Quartz.CFRunLoopStop(self._loop)
