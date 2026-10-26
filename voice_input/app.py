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
from .commands import execute as exec_command
from .commands import match_command
from .hotkey import FnHotkey
from .inject import type_text
from .learn import extract_wrong
from .memory import MemoryStore
from .polish import MARKDOWN_APPS, Polisher
from .recorder import Recorder
from .selection import (frontmost_app_name, frontmost_pid, read_cursor_context,
                        read_selected_text)

log = logging.getLogger(__name__)


class VoiceInputApp:
    def __init__(self, config_path: Path) -> None:
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self._cfg = cfg
        self._config_path = config_path
        vad_model = cfg.get("vad", {}).get("model")
        if vad_model and not Path(vad_model).is_absolute():
            vad_model = str(config_path.parent / vad_model)
        self._recorder = Recorder(vad_model=vad_model)
        self._asr = None  # worker 线程内懒创建（MLX 线程绑定）
        self._polisher = Polisher(**cfg["llm"])
        self._inject_cfg = cfg.get("inject", {})
        mem_cfg = cfg.get("memory", {})
        db_path = Path(mem_cfg.get("db", "data/memory.db")).expanduser()
        if not db_path.is_absolute():
            # 相对路径按 config.yaml 所在目录解析。旧实现是裸相对路径 → 按 CWD 解析，
            # 从别的目录启动就会写到另一份 memory.db，而 `main.py dict` 固定读项目
            # 目录那份，于是命令行看到的和实际用的不是同一个库。
            db_path = config_path.parent / db_path
        self._memory = MemoryStore(db_path)
        from .usage import UsageLogger
        self._usage = UsageLogger(db_path)
        self._polisher.set_usage_logger(self._usage)
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
        # P0 光标上下文：听写进场时捕获（焦点还在目标应用），收尾时随音频入队
        self._cursor_ctx = {"before": "", "after": ""}
        # P2 语音命令（换行/撤销/保存…，整句命中直接发按键）
        self._commands_enabled = cfg.get("commands", True)
        # P1a 支持 Markdown 的 App（内置集合 + 配置追加）
        self._markdown_apps = MARKDOWN_APPS | set(cfg.get("markdown_apps", []))
        # P1b 风格画像
        self._style_cfg = cfg.get("style", {})
        self._ask_card_secs = cfg.get("ask_card_secs", 12)
        self._card = None
        self._last_raw = ""
        self._last_polished = ""
        self._statusbar = None          # run() 里创建（菜单栏图标）
        self._quit = False
        self._autolearn = AutoLearner(self._memory, self._on_learned) \
            if cfg.get("autolearn", {}).get("enabled", True) else None

    def _on_learned(self, wrong: str, right: str) -> None:
        """词条入库后的统一动作：请求回注词典 + HUD 弹出学习反馈。"""
        log.info("学习完成: %s → %s", wrong, right)
        # 回注投到 worker 线程做：ASR session 的 MLX stream 绑定创建线程，
        # 从 autolearn 线程直接 set_context 是跨线程碰它。
        self._queue.put(("_refresh", None, None, None))
        if self._hud is not None:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._hud.showLearned_, f"{wrong} → {right}")

    def _notify(self, text: str) -> None:
        """短提示：HUD 可见 + 落日志。

        旧实现只 print()，而 launchd 下 stdout 落到 wavetype.stdout.log ——
        界面上什么都看不到，学习失败完全静默，这正是"感觉没做好"的来源。
        """
        log.info("%s", text)
        if self._hud is not None:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._hud.showNotice_, text)
        else:
            print(text)

    # ---- 热键回调（event tap 线程） ----

    _MODE_LABEL = {"dictate": "Listening", "translate": "Translating", "ask": "Ask"}

    def _on_press(self, mode: str = "dictate") -> None:
        self._mode = mode
        # Ask：进场时即捕获选区（编辑指令要替换的就是这段文本）。
        # 趁焦点还在目标应用、选区完整时读取，收尾时直接用。
        self._ask_context = read_selected_text(frontmost_pid()) if mode == "ask" else None
        # P0：口述/翻译进场时捕获光标上下文（上文/下文），供润色衔接。
        # 趁焦点还在目标应用读取——收尾时用户可能已切窗口。
        if mode in ("dictate", "translate"):
            self._cursor_ctx = read_cursor_context(frontmost_pid())
        else:
            self._cursor_ctx = {"before": "", "after": ""}
        if self._autolearn is not None:
            self._autolearn.stop()  # 开始新一次听写，停止监视上一段
        self._recorder.start()
        if self._hud is not None:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._hud.setListeningLabel_,
                                self._MODE_LABEL.get(mode, "Listening"))
        if self._statusbar is not None:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._statusbar.set_recording, True, mode)

    def _on_release(self, mode: str = "dictate") -> None:
        audio = self._recorder.stop()
        context, self._ask_context = self._ask_context, None
        cursor_ctx, self._cursor_ctx = self._cursor_ctx, {"before": "", "after": ""}
        if self._statusbar is not None:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._statusbar.set_recording, False, mode)
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
            self._queue.put((audio, mode, context, cursor_ctx))

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
        """Ctrl+Fn：用户已手动改正并选中正确文本 → 学纠错对。

        这是自动学习读不到内容时的兜底（AX 对部分 App 不暴露输入框内容）。
        成功与失败都必须有可见反馈，否则用户只会觉得"按了没反应"。
        """
        selected = read_selected_text(frontmost_pid())
        if not selected:
            self._notify("未读到选区：先选中你改正后的文本，再按 Ctrl+Fn")
            return
        wrong = extract_wrong(self._last_polished, selected)
        if not wrong:
            self._notify(f"与最近一次上屏无明显差异，未入库：{selected[:24]}")
            return
        if not self._memory.add_correction(wrong, selected):
            self._notify(f"未过质量闸门，未入库：{wrong} → {selected}")
            return
        self._on_learned(wrong, selected)

    # ---- worker 线程：ASR + 润色 + 注入 ----

    def _worker(self) -> None:
        while True:
            audio, mode, context, cursor_ctx = self._queue.get()
            try:
                if mode == "_refresh":
                    # 词条入库后的回注（见 _on_learned），不碰音频、不触发模型加载
                    self._refresh_dictionary()
                    self._maybe_distill_style()
                    continue
                if self._asr is None:
                    from .asr import ASR
                    self._asr = ASR(**self._cfg["asr"])
                    self._refresh_dictionary()
                if mode == "_warmup":
                    # 启动预热：一小段静音过一遍转写，让 MLX 完成编译与缓存，
                    # 首次真实听写不再付 2~8s 的加载+冷推理成本
                    self._asr.transcribe(audio)
                    log.info("ASR 预热完成")
                    continue
                self._process(audio, mode, context, cursor_ctx)
            except Exception:
                log.exception("处理失败")

    def _refresh_dictionary(self) -> None:
        if self._asr is None:
            return
        hw = self._memory.hotwords(limit=self._max_hotwords)
        # 空串也要下发：否则把词典删空后，热词会一直残留到下次重启
        self._asr.set_context(hw)
        maps = self._memory.mappings()
        self._polisher.set_dictionary(maps)
        # P1b：风格画像随词典一起回注（没有画像时为空串，prompt 不出现该块）
        profile = self._memory.style_profile()
        self._polisher.set_style_profile(profile)
        log.info("词典已回注（热词 %d 字，映射 %d 条）", len(hw), len(maps))
        if self._statusbar is not None:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._statusbar.set_stats,
                                len(self._memory.list_dictionary()), bool(profile))

    def _maybe_distill_style(self) -> None:
        """P1b：风格编辑攒够增量就重新提炼画像（worker 线程内 LLM 调用）。

        触发条件：style_edits 总数 ≥ 上次提炼样本数 + MIN_SAMPLES（首次 ≥ MIN_SAMPLES）。
        """
        if not self._style_cfg.get("enabled", True):
            return
        from . import style as style_mod
        min_samples = int(self._style_cfg.get("min_samples", style_mod.MIN_SAMPLES))
        meta = self._memory.style_profile_meta()
        baseline = meta[0] if meta else 0
        if self._memory.style_edit_count() < max(min_samples, baseline + min_samples):
            return
        if style_mod.distill(self._memory, self._polisher, min_samples=min_samples):
            self._polisher.set_style_profile(self._memory.style_profile())

    _STAGE_LABEL = {"dictate": "Polishing", "translate": "Translating", "ask": "Asking"}

    def _supports_markdown(self, app: str) -> bool:
        """P1a：前台 App 是否支持 Markdown 输入框（子串匹配，大小写不敏感）。"""
        a = (app or "").lower()
        return any(m.lower() in a for m in self._markdown_apps)

    def _process(self, audio, mode: str = "dictate", context: str | None = None,
                 cursor_ctx: dict | None = None) -> None:
        # 目标应用在注入前就固定下来：后面自动学习要用它做归属校验，
        # 不能用"读回时"的前台应用（那时用户可能已经切走了）
        app, pid = frontmost_app_name(), frontmost_pid()
        ctx = cursor_ctx or {"before": "", "after": ""}
        before, after = ctx.get("before", ""), ctx.get("after", "")
        markdown = self._supports_markdown(app)
        self._stage_label("Transcribing")  # ASR 阶段（首次含模型加载）
        text = self._asr.transcribe(audio)
        if not text:
            # VAD 听到了声音但 ASR 转写为空（说得太轻/太晚/纯噪音）：
            # 必须留日志——静默丢弃会让排障无从下手
            log.info("丢弃：ASR 空结果（%s，%.1fs）", mode, len(audio) / 16000)
            self._hide_hud()
            return
        self._stage_label(self._STAGE_LABEL.get(mode, "Polishing"))  # LLM 阶段

        # P2 语音命令：整句命中（"换行"/"撤销"/"保存"…）直接发按键，不进 LLM。
        # 只在口述模式启用；命中即收尾，不做润色/注入/自动学习。
        if mode == "dictate" and self._commands_enabled:
            hit = match_command(text)
            if hit:
                word, times = hit
                exec_command(word, times)
                self._memory.log_history(f"[命令] {text}", f"{word}×{times}", app)
                self._hide_hud()
                return

        if mode == "translate":
            final = self._polisher.translate(text, app=app, before=before,
                                             after=after, markdown=markdown)
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

        # dictate：ASR → 润色（带光标上下文）→ 上屏
        final = self._polisher.polish(text, app=app, before=before,
                                      after=after, markdown=markdown)
        if not final:
            # 润色层判定"无实义内容"（整句都是口水词）：不上屏、不入历史
            self._hide_hud()
            return
        type_text(final, **self._inject_cfg)
        self._last_raw, self._last_polished = text, final
        self._memory.log_history(text, final, app)
        if self._autolearn is not None:
            # Typeless 式：监视用户改动，自动入库。带 app/pid 做归属校验，
            # 避免切窗口后把另一个输入框的内容当成"用户改了这段"。
            self._autolearn.watch(final, app=app, pid=pid)
        self._hide_hud()

    def _show_card(self, text: str, title: str = "Ask") -> None:
        """主线程展示答案卡片。"""
        if self._card is None:
            return
        from PyObjCTools import AppHelper
        AppHelper.callAfter(lambda: self._card.showAnswer_withTitle_(text, title))

    def _stage_label(self, label: str) -> None:
        """切换 Thinking 阶段的细分标签（主线程执行）。"""
        if self._hud is not None:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._hud.setThinkingLabel_, label)

    # ---- 主线程：NSApp ----

    def run(self) -> None:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        from Foundation import NSTimer
        from PyObjCTools import AppHelper

        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        # 菜单栏图标（Accessory 应用没有 Dock 图标，这是唯一的常驻入口）
        try:
            from .statusbar import StatusBarController
            self._statusbar = StatusBarController(self._config_path)
        except Exception:
            log.exception("菜单栏图标创建失败（不阻塞主流程）")
            self._statusbar = None

        if self._hud_enabled:
            from .hud import AnswerCardController, WaveformHUDController
            self._hud = WaveformHUDController.alloc().initWithLevelProvider_(
                lambda: (self._recorder.level, self._recorder.speech_prob))
            self._card = AnswerCardController.alloc().initWithTimeout_(
                float(self._ask_card_secs))

        threading.Thread(target=self._hotkey.run, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()

        # ASR 预热：worker 线程内先跑一次静音转写，首次真实听写即热
        # （模型与词典常驻内存，与懒加载占用相同，只是把加载挪到启动期）
        import numpy as np
        self._queue.put((np.zeros(16000, dtype=np.float32), "_warmup", None, None))

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
