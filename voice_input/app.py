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
        self._hotkey = FnHotkey(self._on_press, self._on_release,
                                mode=cfg.get("hotkey", {}).get("mode", "hold"),
                                on_correct=self._on_correct)
        self._last_raw = ""
        self._last_polished = ""
        self._quit = False

    # ---- 热键回调（event tap 线程） ----

    def _on_press(self) -> None:
        self._recorder.start()
        if self._hud is not None:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._hud.setListening)

    def _on_release(self) -> None:
        audio = self._recorder.stop()
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
            self._queue.put(audio)

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
            self._refresh_dictionary()
            print(f"📖 已学习：{wrong} → {selected}")
        else:
            print(f"ℹ️  选区与最近输出无明显差异，未入库：{selected}")

    # ---- worker 线程：ASR + 润色 + 注入 ----

    def _worker(self) -> None:
        while True:
            audio = self._queue.get()
            try:
                if self._asr is None:
                    from .asr import ASR
                    self._asr = ASR(**self._cfg["asr"])
                    self._refresh_dictionary()
                self._process(audio)
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

    def _process(self, audio) -> None:
        app = _frontmost_app()
        text = self._asr.transcribe(audio)
        if not text:
            if self._hud is not None:
                from PyObjCTools import AppHelper
                AppHelper.callAfter(self._hud.hide)
            return
        final = self._polisher.polish(text)
        type_text(final, **self._inject_cfg)
        self._last_raw, self._last_polished = text, final
        self._memory.log_history(text, final, app)
        if self._hud is not None:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._hud.hide)  # 上屏完成 → 波形收拢消失

    # ---- 主线程：NSApp ----

    def run(self) -> None:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        from Foundation import NSTimer
        from PyObjCTools import AppHelper

        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        if self._hud_enabled:
            from .hud import WaveformHUDController
            self._hud = WaveformHUDController.alloc().initWithLevelProvider_(
                lambda: (self._recorder.level, self._recorder.speech_prob))

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
        print("🎙️  语音输入已启动：按住 Fn 说话，松开自动上屏；Ctrl+Fn 纠错学习；Ctrl+C 退出")
        AppHelper.runEventLoop()
