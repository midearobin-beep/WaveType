#!/usr/bin/env python3
"""WaveType 首次运行向导（PyObjC 原生 GUI）。

三屏：
  1. 模型选择：云端 DeepSeek（推荐，不占内存）/ 本地 Ollama（离线，约 4GB 内存）
  2. 凭证：云端填 API key 并测试连通；本地检测 Ollama 是否就绪
  3. 快捷键速查 + 权限自检（麦克风 / 辅助功能 / 输入监控）

完成后写入 config.yaml 与 .env（密钥绝不写入 config.yaml）。
运行：python setup_wizard.py   或   python main.py --setup
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import objc
import Quartz
from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSFont,
    NSMakeRect,
    NSScreen,
    NSSecureTextField,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject

ROOT = Path(__file__).resolve().parent
WIN_W, WIN_H = 540.0, 460.0

_GRAY = NSColor.colorWithCalibratedWhite_alpha_(0.55, 1.0)
_TEXT = NSColor.labelColor()
_OK = NSColor.systemGreenColor()
_BAD = NSColor.systemRedColor()


def _label(text, x, y, w, h, size=13.0, bold=False, color=None, wrap=False):
    f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    f.setStringValue_(text)
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    f.setFont_(NSFont.systemFontOfSize_weight_(size, 0.31 if bold else 0.0))
    f.setTextColor_(color or _TEXT)
    if wrap:
        f.setLineBreakMode_(0)
        f.setMaximumNumberOfLines_(0)
        f.setPreferredMaxLayoutWidth_(w)
    return f


def _button(title, x, y, w, h, target, action, primary=False):
    b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    b.setTitle_(title)
    b.setBezelStyle_(1)
    b.setTarget_(target)
    b.setAction_(action)
    if primary:
        b.setKeyEquivalent_("\r")
    return b


def _mic_ok() -> bool:
    """功能性检测：实际打开一次音频输入流。"""
    try:
        import sounddevice as sd
        s = sd.InputStream(samplerate=16000, channels=1, dtype="float32")
        s.start()
        s.stop()
        s.close()
        return True
    except Exception:
        return False


def _ax_ok() -> bool:
    from ApplicationServices import AXIsProcessTrusted
    return bool(AXIsProcessTrusted())


def _input_monitor_ok() -> bool:
    """功能性检测：试着创建一个只读 event tap。"""
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged),
        lambda *a: None, None)
    return bool(tap)


class WizardController(NSObject):
    def init(self):
        self = objc.super(WizardController, self).init()
        if self is None:
            return None
        self._step = 0
        self._provider = "openai"   # openai | ollama
        self._perm_rows = []
        self._build_window()
        self._show_step(0)
        return self

    # ---------- 骨架 ----------

    def _build_window(self):
        screen = NSScreen.mainScreen().visibleFrame()
        x = screen.origin.x + (screen.size.width - WIN_W) / 2.0
        y = screen.origin.y + (screen.size.height - WIN_H) / 2.0
        self._win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, WIN_W, WIN_H),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False)
        self._win.setTitle_("WaveType 设置向导")
        self._content = self._win.contentView()

        self._title = _label("", 24, WIN_H - 44, WIN_W - 48, 22, size=17.0, bold=True)
        self._content.addSubview_(self._title)
        self._step_label = _label("", WIN_W - 90, WIN_H - 42, 66, 18, size=11.0, color=_GRAY)
        self._content.addSubview_(self._step_label)

        self._body = NSView.alloc().initWithFrame_(NSMakeRect(24, 70, WIN_W - 48, WIN_H - 130))
        self._content.addSubview_(self._body)

        self._back = _button("上一步", WIN_W - 224, 20, 96, 30, self, "back:")
        self._next = _button("下一步", WIN_W - 118, 20, 94, 30, self, "next:", primary=True)
        self._content.addSubview_(self._back)
        self._content.addSubview_(self._next)

    def _clear_body(self):
        for v in list(self._body.subviews()):
            v.removeFromSuperview()

    def _show_step(self, n):
        self._step = n
        self._clear_body()
        self._step_label.setStringValue_(f"{n + 1} / 3")
        self._back.setEnabled_(n > 0)
        self._next.setTitle_("完成" if n == 2 else "下一步")
        (self._step_model, self._step_cred, self._step_ready)[n]()

    # ---------- 第 1 屏：模型选择 ----------

    def _step_model(self):
        self._title.setStringValue_("选择润色模型")
        w = self._body.frame().size.width

        opt1 = NSButton.alloc().initWithFrame_(NSMakeRect(0, 210, w, 26))
        opt1.setButtonType_(3)  # radio
        opt1.setTitle_("云端模型（推荐）— DeepSeek API")
        opt1.setTarget_(self)
        opt1.setAction_("pickCloud:")
        opt1.setState_(1 if self._provider == "openai" else 0)
        self._body.addSubview_(opt1)
        self._body.addSubview_(_label(
            "不占内存、无冷启动（本地 4B 模型首句约 30s）。音频始终不出本机，只有转写文本上行。",
            20, 176, w - 20, 34, size=11.0, color=_GRAY, wrap=True))

        opt2 = NSButton.alloc().initWithFrame_(NSMakeRect(0, 140, w, 26))
        opt2.setButtonType_(3)
        opt2.setTitle_("本地模型 — Ollama（完全离线）")
        opt2.setTarget_(self)
        opt2.setAction_("pickLocal:")
        opt2.setState_(1 if self._provider == "ollama" else 0)
        self._body.addSubview_(opt2)
        self._body.addSubview_(_label(
            "零成本、纯离线；约占 4GB 内存，闲置 5 分钟卸载后再次使用需重新加载。",
            20, 106, w - 20, 34, size=11.0, color=_GRAY, wrap=True))

        self._body.addSubview_(_label(
            "ASR 语音识别在两种方案下都始终运行在本机（MLX 本地模型）。",
            0, 56, w, 30, size=11.0, color=_GRAY, wrap=True))
        self._opt1, self._opt2 = opt1, opt2

    def pickCloud_(self, s):
        self._provider = "openai"
        self._opt1.setState_(1)
        self._opt2.setState_(0)

    def pickLocal_(self, s):
        self._provider = "ollama"
        self._opt1.setState_(0)
        self._opt2.setState_(1)

    # ---------- 第 2 屏：凭证 ----------

    def _step_cred(self):
        w = self._body.frame().size.width
        if self._provider == "openai":
            self._title.setStringValue_("云端 API Key")
            self._body.addSubview_(_label(
                "在 platform.deepseek.com 创建 API key，粘贴到下方（只存进 .env，不上库）：",
                0, 210, w, 30, size=12.0, color=_GRAY, wrap=True))
            key = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 168, w - 110, 26))
            key.setPlaceholderString_("sk-...")
            self._body.addSubview_(key)
            self._key_field = key
            # 已有 key 则预填（不覆盖用户已有配置）
            env = ROOT / ".env"
            if env.is_file():
                for line in env.read_text().splitlines():
                    if line.startswith("DEEPSEEK_API_KEY="):
                        key.setStringValue_(line.split("=", 1)[1].strip().strip('"').strip("'"))
            self._test_btn = _button("测试连接", w - 100, 166, 100, 28, self, "testKey:")
            self._body.addSubview_(self._test_btn)
            self._cred_status = _label("", 0, 128, w, 30, size=12.0, wrap=True)
            self._body.addSubview_(self._cred_status)
        else:
            self._title.setStringValue_("本地 Ollama 检测")
            self._body.addSubview_(_label(
                "本地模型需要 Ollama 服务。如未安装：brew install ollama && brew services start ollama",
                0, 210, w, 30, size=12.0, color=_GRAY, wrap=True))
            self._test_btn = _button("检测 Ollama", 0, 168, 120, 28, self, "testKey:")
            self._body.addSubview_(self._test_btn)
            self._cred_status = _label("", 0, 128, w, 44, size=12.0, wrap=True)
            self._body.addSubview_(self._cred_status)

    def testKey_(self, s):
        self._cred_status.setTextColor_(_GRAY)
        self._cred_status.setStringValue_("检测中…")
        if self._provider == "openai":
            key = self._key_field.stringValue().strip()
            if not key.startswith("sk-"):
                self._cred_status.setTextColor_(_BAD)
                self._cred_status.setStringValue_("key 格式不对（应以 sk- 开头）")
                return
            try:
                import httpx
                r = httpx.get("https://api.deepseek.com/v1/models",
                              headers={"Authorization": f"Bearer {key}"},
                              timeout=15.0, trust_env=False)
                if r.status_code == 200:
                    self._cred_status.setTextColor_(_OK)
                    self._cred_status.setStringValue_("✓ 连接成功，密钥有效")
                else:
                    self._cred_status.setTextColor_(_BAD)
                    self._cred_status.setStringValue_(f"✗ HTTP {r.status_code}：密钥无效或额度不足")
            except Exception as e:
                self._cred_status.setTextColor_(_BAD)
                self._cred_status.setStringValue_(f"✗ 网络不通：{type(e).__name__}")
        else:
            if not shutil.which("ollama"):
                self._cred_status.setTextColor_(_BAD)
                self._cred_status.setStringValue_("✗ 未找到 ollama，请先 brew install ollama")
                return
            try:
                out = subprocess.run(["ollama", "list"], capture_output=True,
                                     text=True, timeout=10).stdout
                if "qwen3.5" in out:
                    self._cred_status.setTextColor_(_OK)
                    self._cred_status.setStringValue_("✓ Ollama 运行中，qwen3.5 模型已就位")
                else:
                    self._cred_status.setTextColor_(_BAD)
                    self._cred_status.setStringValue_("Ollama 在运行，但缺模型：ollama pull qwen3.5:4b-mlx")
            except Exception as e:
                self._cred_status.setTextColor_(_BAD)
                self._cred_status.setStringValue_(f"✗ 检测失败：{type(e).__name__}")

    # ---------- 第 3 屏：快捷键 + 权限 ----------

    def _step_ready(self):
        self._title.setStringValue_("快捷键与权限自检")
        w = self._body.frame().size.width

        keys = [("Fn", "口述：按住说话，松开/再按收尾"),
                ("Fn + Shift", "中英互译（说中文出英文，反之亦然）"),
                ("Fn + Space", "语音问答；选中文本后可说「把这段改成…」"),
                ("Ctrl + Fn", "纠错学习：选中改正后的文本按下")]
        y = 236
        for combo, desc in keys:
            chip = _label(combo, 0, y, 110, 20, size=12.0, bold=True)
            self._body.addSubview_(chip)
            self._body.addSubview_(_label(desc, 118, y, w - 118, 20, size=12.0, color=_GRAY))
            y -= 26

        self._perm_rows = []
        y -= 14
        for name, check, settings_hint in (
                ("麦克风", _mic_ok, "隐私与安全性 → 麦克风"),
                ("辅助功能", _ax_ok, "隐私与安全性 → 辅助功能"),
                ("输入监控", _input_monitor_ok, "隐私与安全性 → 输入监控")):
            status = _label("…", 0, y, 22, 20, size=13.0)
            ok = check()
            status.setStringValue_("✓" if ok else "✗")
            status.setTextColor_(_OK if ok else _BAD)
            self._body.addSubview_(status)
            self._body.addSubview_(_label(
                name if ok else f"{name}（到 系统设置 → {settings_hint} 授权本终端）",
                30, y, w - 130, 20, size=12.0,
                color=_TEXT if ok else _BAD))
            self._perm_rows.append(status)
            y -= 26

        self._body.addSubview_(_label(
            "授权需重启本程序后生效。「按下 Fn 键时」请在 系统设置 → 键盘 中设为「什么都不做」。",
            0, 8, w, 34, size=11.0, color=_GRAY, wrap=True))

    # ---------- 导航与落地 ----------

    def back_(self, s):
        self._show_step(self._step - 1)

    def next_(self, s):
        if self._step < 2:
            self._show_step(self._step + 1)
        else:
            self._finish()

    def _finish(self):
        cfg = (ROOT / "config.yaml")
        if not cfg.is_file():
            text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
            if self._provider == "ollama":
                text = text.replace("provider: openai", "provider: ollama") \
                           .replace("base_url: https://api.deepseek.com",
                                    "base_url: http://127.0.0.1:11434") \
                           .replace("model: deepseek-flash", "model: qwen3.5:4b-mlx")
            cfg.write_text(text, encoding="utf-8")
        if self._provider == "openai" and getattr(self, "_key_field", None) is not None:
            key = self._key_field.stringValue().strip()
            if key.startswith("sk-"):
                (ROOT / ".env").write_text(f"DEEPSEEK_API_KEY={key}\n", encoding="utf-8")
        self._win.close()
        from AppKit import NSApp
        NSApp.terminate_(None)
        print("✅ 配置完成。运行 ./start.sh 启动 WaveType。")


def run() -> None:
    from AppKit import NSApplicationActivationPolicyRegular
    from PyObjCTools import AppHelper
    app = NSApplication.sharedApplication()
    # 向导是前台交互窗口：Regular 策略 + 激活到最前（终端启动时默认没焦点）
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    WizardController.alloc().init()
    app.activateIgnoringOtherApps_(True)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    sys.exit(run())
