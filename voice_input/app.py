"""主流程：Fn 按住说话 → ASR → LLM 润色 → 逐字注入；HUD 波形浮层。

线程模型（v2，接入 HUD 后）：
- 主线程：NSApplication 事件循环（HUD 渲染、定时器、退出轮询）
- 后台线程 A：Quartz event tap（CFRunLoop 监听 Fn）
- 后台线程 B：处理管线（ASR + LLM + 注入）

注意：MLX 的 GPU stream 绑定**创建**线程。ASR Session 在 worker 线程内
懒创建，推理也只在 worker 线程 —— 满足同线程约束，同时不阻塞 UI。
HUD 的 show/hide 必须从 event tap 线程 marshal 回主线程（AppHelper.callAfter）。
"""
from __future__ import annotations

import logging
import queue
import signal
import threading
from pathlib import Path

import yaml

from .autolearn import AutoLearner
from .hotkey import FnHotkey
from .inject import type_text
from .learn import extract_wrong
from .memory import MemoryStore
from .polish import Polisher
from .recorder import Recorder
from .selection import read_selected_text

log = logging.getLogger(__name__)


def _frontmost_app() -> str:
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return app.localizedName() if app else ""
    except Exception:
        return ""


class VoiceInputApp:
    def __init__(self, config_path: Path) -> None:
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self._cfg = cfg
        vad_model = cfg.get("vad", {}).get("model")
        if vad_model and not Path(vad_model).is_absolute():
            vad_model = str(config_path.parent / vad_model)
        self._recorder = Recorder(vad_model=vad_model)
        self._asr = None  # worker 线程内懒创建（MLX 线程绑定）
        self._polisher = Polisher(**cfg["llm"])
        self._inject_cfg = cfg.get("inject", {})
        mem_cfg = cfg.get("memory", {})
        self._memory = MemoryStore(mem_cfg.get(
            "db", str(config_path.parent / "data" / "memory.db")))
        self._max_hotwords = mem_cfg.get("max_hotwords", 80)
        self._hud_enabled = cfg.get("hud", {}).get("enabled", True)
        self._hud = None
        self._queue: queue.Queue = queue.Queue()
        modes_cfg = cfg.get("modes", {})
        self._hotkey = FnHotkey(self._on_press, self._on_release,
                                mode=cfg.get("hotkey", {}).get("mode", "hold"),
                                on_correct=self._on_correct,
                                on_cancel=self._on_cancel,
                                translate_enabled=modes_cfg.get("translate", True),
                                ask_enabled=modes_cfg.get("ask", True))
        self._mode = "dictate"          # 当前录音模式
        self._ask_context = None        # Ask 进场时捕获的选区文本
        self._ask_card_secs = cfg.get("ask_card_secs", 12)
        self._card = None
        self._last_raw = ""
        self._last_polished = ""
        self._quit = False
        self._autolearn = AutoLearner(self._memory, self._on_learned) \
            if cfg.get("autolearn", {}).get("enabled", True) else None

    def _on_learned(self, wrong: str, right: str) -> None:
        """词条入库后的统一动作：回注词典 + HUD 弹出学习反馈。"""
        self._refresh_dictionary()
        if self._hud is not None:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._hud.showLearned_, f"{wrong} → {right}")

    # ---- 热键回调（event tap 线程） ----

    _MODE_LABEL = {"dictate": "Listening", "translate": "Translating", "ask": "Ask"}

    def _on_press(self, mode: str = "dictate") -> None:
        self._mode = mode
        # Ask：进场时即捕获选区（编辑指令要替换的就是这段文本）。
        # 趁焦点还在目标应用、选区完整时读取，收尾时直接用。
        self._ask_context = read_selected_text() if mode == "ask" else None
        if self._autolearn is not None:
            self._autolearn.stop()  # 开始新一次听写，停止监视上一段
        self._recorder.start()
        if self._hud is not None:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._hud.setListeningLabel_,
                                self._MODE_LABEL.get(mode, "Listening"))

    def _on_release(self, mode: str = "dictate") -> None:
        audio = self._recorder.stop()
        context, self._ask_context = self._ask_context, None
        max_speech = self._recorder.max_speech_prob
        # 防幻觉两道防线（热词偏置下，静音/噪音会被 ASR 幻觉成词典词）：
        # ① 录音太短（误触）② VAD 全程没检测到人声 —— 直接收拢 HUD，不进 Thinking
        duration = len(audio) / 16000
        if len(audio) and duration < 0.5:
            log.info("丢弃：录音过短 (%.2fs)", duration)
            self._hide_hud()
            return
        if len(audio) and self._recorder._vad is not None and max_speech < 0.4:
            log.info("丢弃：未检测到人声（max prob %.2f）", max_speech)
            self._hide_hud()
            return
        if self._hud is not None:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._hud.setThinking)  # 录音结束→模型处理中
        if len(audio):
            self._queue.put((audio, mode, context))

    def _on_cancel(self) -> None:
        """Fn 先落的 Fn+Shift chord：口述刚启动即作废，改走翻译。"""
        self._recorder.stop()  # 丢弃这段刚开始的录音
        self._ask_context = None
        log.info("口述已取消（升级为翻译模式）")

    def _hide_hud(self) -> None:
        if self._hud is not None:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._hud.hide)

    def _on_correct(self) -> None:
        """Ctrl+Fn：用户已手动改正并选中正确文本 → 学纠错对。"""
        selected = read_selected_text()
        if not selected:
            print("⚠️  未读到选区：先选中你改正后的文本，再按 Ctrl+Fn")
            return
        wrong = extract_wrong(self._last_polished, selected)
        if wrong:
            self._memory.add_correction(wrong, selected)
            self._on_learned(wrong, selected)
            print(f"📖 已学习：{wrong} → {selected}")
        else:
            print(f"ℹ️  选区与最近输出无明显差异，未入库：{selected}")

    # ---- worker 线程：ASR + 润色 + 注入 ----

    def _worker(self) -> None:
        while True:
            audio, mode, context = self._queue.get()
            try:
                if self._asr is None:
                    from .asr import ASR
                    self._asr = ASR(**self._cfg["asr"])
                    self._refresh_dictionary()
                self._process(audio, mode, context)
            except Exception:
                log.exception("处理失败")

    def _refresh_dictionary(self) -> None:
        if self._asr is None:
            return
        hw = self._memory.hotwords(limit=self._max_hotwords)
        if hw:
            self._asr.set_context(hw)
        self._polisher.set_dictionary(self._memory.mappings())
        log.info("词典已回注（热词 %d 字）", len(hw))

    def _process(self, audio, mode: str = "dictate", context: str | None = None) -> None:
        app = _frontmost_app()
        text = self._asr.transcribe(audio)
        if not text:
            # VAD 听到了声音但 ASR 转写为空（说得太轻/太晚/纯噪音）：
            # 必须留日志——静默丢弃会让排障无从下手
            log.info("丢弃：ASR 空结果（%s，%.1fs）", mode, len(audio) / 16000)
            self._hide_hud()
            return

        if mode == "translate":
            final = self._polisher.translate(text, app=app)
            type_text(final, **self._inject_cfg)
            self._memory.log_history(f"[翻译] {text}", final, app)
            self._hide_hud()
            return

        if mode == "ask":
            action, content = self._polisher.ask(text, context, app=app)
            if action == "edit" and context:
                # 编辑指令：选区仍在焦点应用里保持选中，直接打字即整体替换
                type_text(content, **self._inject_cfg)
                self._memory.log_history(f"[改写] {text}", content, app)
                self._hide_hud()
            elif content:
                # 问答：答案进卡片，不动用户文本
                self._memory.log_history(f"[问答] {text}", content, app)
                self._hide_hud()
                self._show_card(content, title="Ask")
            else:
                self._memory.log_history(f"[问答] {text}", "(调用失败)", app)
                self._hide_hud()
                self._show_card("云端服务暂时不可用，请稍后再试。", title="Ask")
            return

        # dictate：ASR → 润色 → 上屏
        final = self._polisher.polish(text, app=app)
        if not final:
            # 润色层判定"无实义内容"（整句都是口水词）：不上屏、不入历史
            self._hide_hud()
            return
        type_text(final, **self._inject_cfg)
        self._last_raw, self._last_polished = text, final
        self._memory.log_history(text, final, app)
        if self._autolearn is not None:
            self._autolearn.watch(final)  # Typeless 式：监视用户改动，自动入库
        self._hide_hud()

    def _show_card(self, text: str, title: str = "Ask") -> None:
        """主线程展示答案卡片。"""
        if self._card is None:
            return
        from PyObjCTools import AppHelper
        AppHelper.callAfter(lambda: self._card.showAnswer_withTitle_(text, title))

    # ---- 主线程：NSApp ----

    def run(self) -> None:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        from Foundation import NSTimer
        from PyObjCTools import AppHelper

        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        if self._hud_enabled:
            from .hud import AnswerCardController, WaveformHUDController
            self._hud = WaveformHUDController.alloc().initWithLevelProvider_(
                lambda: (self._recorder.level, self._recorder.speech_prob))
            self._card = AnswerCardController.alloc().initWithTimeout_(
                float(self._ask_card_secs))

        threading.Thread(target=self._hotkey.run, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()

        # Ctrl+C：信号处理器只置标志，主线程由定时轮询执行退出
        signal.signal(signal.SIGINT, lambda *a: setattr(self, "_quit", True))

        def _poll_quit():
            if self._quit:
                app.terminate_(None)
                return
            AppHelper.callAfter(0.4, _poll_quit)

        _poll_quit()
        print("🎙️  语音输入已启动：")
        print("    Fn          口述（按住说话 / 再按收尾）")
        print("    Fn + Shift  中英互译（点按进场，Fn 收尾）")
        print("    Fn + Space  语音问答（选中文本时可语音编辑）")
        print("    Ctrl + Fn   纠错学习 · Ctrl+C 退出")
        AppHelper.runEventLoop()
