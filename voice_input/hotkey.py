"""全局热键：Quartz event tap 监听 Fn 及其组合键（Typeless 风格）。

Fn 在 macOS 上不是普通按键，走 flagsChanged 事件，
标志位 kCGEventFlagMaskSecondaryFn (0x800000) 表示按下状态。

组合键（统一"组合键进场、Fn 收尾"）：
- Fn            ：口述（hold 按住说话 / toggle 点按开关）
- Fn + Shift    ：翻译模式（点按进场，说话，点按 Fn 收尾）
- Fn + Space    ：Ask 模式（点按进场，说话，点按 Fn 收尾）
- Ctrl + Fn     ：纠错学习（读选区 → 学纠错对）

实现注意：
- Fn+Space 中的 Space 是普通按键（keyDown）。tap 必须订阅 keyDown/keyUp
  并**吞掉**这对事件（否则空格会漏进当前输入框），因此 tap 使用
  kCGEventTapOptionDefault（可过滤）而非 ListenOnly。
- Fn+Shift 是修饰键组合，用户可能 Shift 先落或 Fn 先落。Fn 先落时会先触发
  口述；若 Shift 在 0.6s 内紧跟落下，则取消这次口述、改走翻译（chord 纠错）。
- 系统设置 → 键盘 →「按下 Fn 键时」需设为「什么都不做」，
  否则 Fn 会被系统听写/输入法切换抢走。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

import Quartz

log = logging.getLogger(__name__)

_FN_FLAG = 0x800000    # kCGEventFlagMaskSecondaryFn
_SHIFT_FLAG = 0x20000  # kCGEventFlagMaskShift
_CTRL_FLAG = 0x40000   # kCGEventFlagMaskControl
_SPACE_KEYCODE = 0x31
_CHORD_GRACE_S = 0.6   # Fn 先落、Shift 后至的组合键纠错窗口


class FnHotkey:
    """Fn 口述 + Fn+Shift 翻译 + Fn+Space 问答 + Ctrl+Fn 纠错捕获。

    回调签名：
    - on_press(mode)   mode ∈ "dictate" | "translate" | "ask"
    - on_release(mode) 录音结束，按 mode 走对应管线
    - on_cancel()      口述刚启动就被升级为翻译（Fn 先落的 Fn+Shift chord）
    - on_correct()     Ctrl+Fn 纠错学习
    """

    def __init__(self, on_press: Callable[[str], None],
                 on_release: Callable[[str], None],
                 mode: str = "hold",
                 on_correct: Callable[[], None] | None = None,
                 on_cancel: Callable[[], None] | None = None,
                 translate_enabled: bool = True,
                 ask_enabled: bool = True) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._on_correct = on_correct
        self._on_cancel = on_cancel
        self._mode = mode
        self._translate_enabled = translate_enabled
        self._ask_enabled = ask_enabled
        self._fn_down = False
        self._shift_down = False
        self._active: str | None = None    # 当前录音模式（None = 空闲）
        self._active_since = 0.0           # 当前模式启动时刻（chord 纠错窗口用）
        self._space_swallowed = False      # Fn+Space 的 keyUp 也要一并吞掉
        self._loop = None
        # 回调工作队列：tap 回调里只入队不干活。录音启动、选区读取（含 0.15s
        # 剪贴板兜底）都是毫秒~百毫秒级阻塞，直接在 tap 线程跑会被系统判定
        # 超时并禁用 tap（实测已发生），期间按键丢失。
        self._events: queue.Queue = queue.Queue()
        threading.Thread(target=self._event_loop, daemon=True).start()

    def _event_loop(self) -> None:
        while True:
            fn, args = self._events.get()
            try:
                fn(*args)
            except Exception:
                log.exception("热键回调异常")

    # ---------- 事件回调 ----------

    def _callback(self, proxy, event_type, event, refcon):
        if event_type == Quartz.kCGEventTapDisabledByTimeout:
            log.warning("event tap 超时被系统禁用，重新启用")
            Quartz.CGEventTapEnable(self._tap, True)
            return event

        if event_type in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
            return self._on_key(event_type, event)

        if event_type == Quartz.kCGEventFlagsChanged:
            self._on_flags(event)
        return event

    def _on_key(self, event_type, event):
        """Fn+Space 检测与吞事件。"""
        keycode = Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode)
        if keycode != _SPACE_KEYCODE:
            return event
        if event_type == Quartz.kCGEventKeyDown:
            flags = Quartz.CGEventGetFlags(event)
            if self._ask_enabled and (flags & _FN_FLAG):
                # Fn+Space 的正常手法是先按住 Fn 再敲 Space：Fn 落下沿会先把
                # 口述启动，Space 晚几十~几百毫秒才到——与 Fn+Shift 一样做
                # chord 纠错：在宽限窗口内取消口述、升级为 Ask。
                if (self._active == "dictate"
                        and time.monotonic() - self._active_since < _CHORD_GRACE_S):
                    log.debug("Fn+Space（Fn 先落）→ 取消口述改走 ask")
                    self._cancel_dictate()
                log.debug("Fn+Space → ask")
                self._space_swallowed = True
                self._start("ask")
                return None  # 吞掉，不让空格打进输入框
            return event
        # keyUp：若刚才的 keyDown 被吞，配套吞掉 keyUp
        if self._space_swallowed:
            self._space_swallowed = False
            return None
        return event

    def _on_flags(self, event) -> None:
        flags = Quartz.CGEventGetFlags(event)
        fn = bool(flags & _FN_FLAG)
        shift = bool(flags & _SHIFT_FLAG)
        ctrl = bool(flags & _CTRL_FLAG)
        log.debug("flagsChanged flags=%#x fn=%s shift=%s", flags, fn, shift)

        # Shift 按下沿（Fn 已按住）：
        if shift and not self._shift_down and fn and self._translate_enabled:
            if self._active is None:
                log.debug("Fn+Shift → translate")
                self._start("translate")
            elif (self._active == "dictate"
                  and time.monotonic() - self._active_since < _CHORD_GRACE_S):
                # Fn 先落的 chord：刚开的口述取消，升级为翻译
                log.debug("Fn+Shift（Fn 先落）→ 取消口述改走 translate")
                self._cancel_dictate()
                self._start("translate")

        # Fn 沿变化
        if fn != self._fn_down:
            if fn:  # Fn 按下沿
                if ctrl and self._on_correct is not None:
                    self._safe_call(self._on_correct)
                elif self._active is not None:
                    self._finish()          # 任意模式：Fn 收尾
                elif shift and self._translate_enabled:
                    self._start("translate")  # Shift 已按住，Fn 后落的 chord
                else:
                    self._start("dictate")
            else:  # Fn 松开沿：仅 hold 模式的口述在此收尾
                if self._mode == "hold" and self._active == "dictate":
                    self._finish()

        self._fn_down = fn
        self._shift_down = shift

    # ---------- 模式状态机 ----------

    def _start(self, mode: str) -> None:
        if self._active is not None:
            return  # 录音中不响应新进场组合键
        # hold 模式下口述按住说话；translate/ask 恒为点按进场
        self._active = mode
        self._active_since = time.monotonic()
        self._safe_call(self._on_press, mode)

    def _finish(self) -> None:
        mode, self._active = self._active, None
        if mode is not None:
            self._safe_call(self._on_release, mode)

    def _cancel_dictate(self) -> None:
        self._active = None
        if self._on_cancel is not None:
            self._safe_call(self._on_cancel)

    def _safe_call(self, fn, *args) -> None:
        """回调一律走工作队列，tap 线程即刻返回（防超时禁用）。"""
        self._events.put((fn, args))

    # ---------- tap 生命周期 ----------

    def run(self) -> None:
        """在当前线程跑 event tap（需要 CFRunLoop，建议放主线程）。"""
        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
                | Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
                | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp))
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,  # 可过滤：吞 Fn+Space 需要
            mask,
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
        log.info("热键监听已启动（mode=%s, translate=%s, ask=%s）",
                 self._mode, self._translate_enabled, self._ask_enabled)
        Quartz.CFRunLoopRun()

    def stop(self) -> None:
        if self._loop is not None:
            Quartz.CFRunLoopStop(self._loop)
