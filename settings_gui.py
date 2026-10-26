#!/usr/bin/env python3
"""WaveType 设置界面（PyObjC 原生 GUI）。

一块窗口搞定日常设置：
- 润色模型：云端 OpenAI 兼容 API（base_url / model / key + 测试连接）
            或本地 Ollama（模型下拉自动取 ollama list / keep_alive）
- Token 用量：今日 / 累计 / 按功能（润色、翻译、问答）分项统计

保存写回 config.yaml 的 llm 段（精准替换，保留其余内容与注释）；
API key 只写 .env，绝不进 config.yaml。

运行：python main.py --settings   或   python settings_gui.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import objc
from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSComboBox,
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
WIN_W, WIN_H = 560.0, 620.0

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


def _field(text, x, y, w, h=26, secure=False):
    cls = NSSecureTextField if secure else NSTextField
    f = cls.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    f.setStringValue_(text)
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


def _load_config() -> dict:
    import yaml
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.is_file():
        return {}
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}


def _load_env_key() -> str:
    env = ROOT / ".env"
    if not env.is_file():
        return ""
    for line in env.read_text().splitlines():
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _ollama_models() -> list[str]:
    if not shutil.which("ollama"):
        return []
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True,
                             text=True, timeout=10).stdout
        return [ln.split()[0] for ln in out.splitlines()[1:] if ln.strip()]
    except Exception:
        return []


def _save_llm_config(provider: str, base_url: str, model: str,
                     keep_alive: str, api_key: str) -> None:
    """精准替换 config.yaml 的 llm 段活动行（注释与其余段落原样保留）。"""
    cfg = ROOT / "config.yaml"
    lines = cfg.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("llm:"))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i] and lines[i][0] not in " #"), len(lines))

    new_kv = {"provider": provider, "base_url": base_url, "model": model}
    if provider == "ollama":
        new_kv["think"] = "false"
        new_kv["keep_alive"] = keep_alive or "30m"

    seen = set()
    model_line_idx = None
    for i in range(start + 1, end):
        m = re.match(r"^  (\w+):", lines[i])  # 只匹配活动行（缩进两格、非注释）
        if not m:
            continue
        key = m.group(1)
        if key == "model":
            model_line_idx = i
        if key in new_kv:
            lines[i] = f"  {key}: {new_kv[key]}"
            seen.add(key)
    # ollama 专有键若原配置没有活动行，插在 model 行之后
    missing = [k for k in ("think", "keep_alive")
               if k in new_kv and k not in seen]
    if missing and model_line_idx is not None:
        insert_at = model_line_idx + 1
        for k in missing:
            lines.insert(insert_at, f"  {k}: {new_kv[k]}")
            insert_at += 1
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if provider == "openai" and api_key:
        (ROOT / ".env").write_text(f"DEEPSEEK_API_KEY={api_key}\n", encoding="utf-8")


class SettingsController(NSObject):
    def init(self):
        self = objc.super(SettingsController, self).init()
        if self is None:
            return None
        cfg = _load_config()
        llm = cfg.get("llm", {})
        self._provider = llm.get("provider", "openai")
        self._cfg = cfg
        self._build_window()
        self._load_values(llm)
        self._refresh_usage()
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
        self._win.setTitle_("WaveType 设置")
        c = self._win.contentView()
        w = WIN_W - 48

        c.addSubview_(_label("润色模型", 24, WIN_H - 46, w, 22, size=17.0, bold=True))

        # 提供方切换
        self._radio_cloud = NSButton.alloc().initWithFrame_(NSMakeRect(24, WIN_H - 78, 240, 24))
        self._radio_cloud.setButtonType_(3)
        self._radio_cloud.setTitle_("云端 API（OpenAI 兼容）")
        self._radio_cloud.setTarget_(self)
        self._radio_cloud.setAction_("pickCloud:")
        c.addSubview_(self._radio_cloud)
        self._radio_local = NSButton.alloc().initWithFrame_(NSMakeRect(280, WIN_H - 78, 240, 24))
        self._radio_local.setButtonType_(3)
        self._radio_local.setTitle_("本地模型（Ollama，离线）")
        self._radio_local.setTarget_(self)
        self._radio_local.setAction_("pickLocal:")
        c.addSubview_(self._radio_local)

        # ---- 云端设置区 ----
        self._cloud_box = NSView.alloc().initWithFrame_(NSMakeRect(24, WIN_H - 268, w, 172))
        cw = w
        self._cloud_box.addSubview_(_label("Base URL", 0, 144, 90, 20, size=12.0, color=_GRAY))
        self._f_base = _field("", 92, 142, cw - 92)
        self._cloud_box.addSubview_(self._f_base)
        self._cloud_box.addSubview_(_label("模型", 0, 108, 90, 20, size=12.0, color=_GRAY))
        self._f_model = _field("", 92, 106, cw - 92)
        self._cloud_box.addSubview_(self._f_model)
        self._cloud_box.addSubview_(_label("API Key", 0, 72, 90, 20, size=12.0, color=_GRAY))
        self._f_key = _field("", 92, 70, cw - 202, secure=True)
        self._cloud_box.addSubview_(self._f_key)
        self._btn_test_cloud = _button("测试连接", cw - 100, 68, 100, 28, self, "testCloud:")
        self._cloud_box.addSubview_(self._btn_test_cloud)
        self._cloud_status = _label("", 0, 30, cw, 34, size=12.0, wrap=True)
        self._cloud_box.addSubview_(self._cloud_status)
        self._cloud_box.addSubview_(_label(
            "密钥只写入项目根目录 .env（已 gitignore），不进 config.yaml。",
            0, 4, cw, 22, size=11.0, color=_GRAY))
        c.addSubview_(self._cloud_box)

        # ---- 本地设置区 ----
        self._local_box = NSView.alloc().initWithFrame_(NSMakeRect(24, WIN_H - 268, w, 172))
        self._local_box.addSubview_(_label("模型", 0, 144, 90, 20, size=12.0, color=_GRAY))
        self._f_omodel = NSComboBox.alloc().initWithFrame_(NSMakeRect(92, 142, cw - 202, 26))
        self._f_omodel.addItemsWithObjectValues_(_ollama_models())
        self._local_box.addSubview_(self._f_omodel)
        self._btn_reload_models = _button("刷新列表", cw - 100, 140, 100, 28, self, "reloadModels:")
        self._local_box.addSubview_(self._btn_reload_models)
        self._local_box.addSubview_(_label("keep_alive", 0, 108, 90, 20, size=12.0, color=_GRAY))
        self._f_keep = _field("", 92, 106, 120)
        self._local_box.addSubview_(self._f_keep)
        self._local_box.addSubview_(_label(
            "模型驻留时长，避免闲置卸载后首句重新加载（约 30s）。",
            222, 108, cw - 222, 20, size=11.0, color=_GRAY))
        self._btn_test_local = _button("检测 Ollama", 92, 66, 120, 28, self, "testLocal:")
        self._local_box.addSubview_(self._btn_test_local)
        self._local_status = _label("", 0, 26, cw, 34, size=12.0, wrap=True)
        self._local_box.addSubview_(self._local_status)
        c.addSubview_(self._local_box)

        # ---- Token 用量区 ----
        ty = WIN_H - 300
        c.addSubview_(_label("Token 用量", 24, ty, 200, 20, size=14.0, bold=True))
        self._btn_refresh = _button("刷新", WIN_W - 114, ty - 4, 90, 26, self, "refreshUsage:")
        c.addSubview_(self._btn_refresh)
        self._usage_today = _label("", 24, ty - 30, w, 20, size=12.0)
        self._usage_total = _label("", 24, ty - 52, w, 20, size=12.0)
        c.addSubview_(self._usage_today)
        c.addSubview_(self._usage_total)
        self._usage_feats = []
        for i in range(3):
            lbl = _label("", 40, ty - 78 - i * 22, w - 16, 20, size=12.0, color=_GRAY)
            c.addSubview_(lbl)
            self._usage_feats.append(lbl)
        c.addSubview_(_label(
            "云端取 API 返回的 usage 字段；本地取 Ollama 的 eval_count。只统计 LLM 文本，"
            "ASR 语音识别在本机运行、无 token 概念。",
            24, ty - 152, w, 40, size=11.0, color=_GRAY, wrap=True))

        # ---- 底部 ----
        self._save_status = _label("", 24, 22, 300, 22, size=12.0, wrap=True)
        c.addSubview_(self._save_status)
        c.addSubview_(_button("保存", WIN_W - 224, 18, 96, 30, self, "save:", primary=True))
        c.addSubview_(_button("关闭", WIN_W - 118, 18, 94, 30, self, "closeWindow:"))

    def _load_values(self, llm: dict):
        self._radio_cloud.setState_(1 if self._provider == "openai" else 0)
        self._radio_local.setState_(1 if self._provider == "ollama" else 0)
        self._f_base.setStringValue_(llm.get("base_url", "https://api.deepseek.com"))
        self._f_model.setStringValue_(llm.get("model", "deepseek-flash"))
        self._f_key.setStringValue_(_load_env_key())
        self._f_omodel.setStringValue_(llm.get("model", "qwen3.5:4b-mlx")
                                       if self._provider == "ollama" else "qwen3.5:4b-mlx")
        self._f_keep.setStringValue_(llm.get("keep_alive", "30m"))
        self._sync_boxes()

    def _sync_boxes(self):
        self._cloud_box.setHidden_(self._provider != "openai")
        self._local_box.setHidden_(self._provider != "ollama")

    # ---------- 提供方切换 ----------

    def pickCloud_(self, s):
        self._provider = "openai"
        self._radio_cloud.setState_(1)
        self._radio_local.setState_(0)
        self._sync_boxes()

    def pickLocal_(self, s):
        self._provider = "ollama"
        self._radio_cloud.setState_(0)
        self._radio_local.setState_(1)
        self._sync_boxes()

    # ---------- 测试 ----------

    def testCloud_(self, s):
        self._cloud_status.setTextColor_(_GRAY)
        self._cloud_status.setStringValue_("检测中…")
        key = self._f_key.stringValue().strip()
        base = self._f_base.stringValue().strip().rstrip("/")
        if not key.startswith("sk-"):
            self._cloud_status.setTextColor_(_BAD)
            self._cloud_status.setStringValue_("key 格式不对（应以 sk- 开头）")
            return
        try:
            import httpx
            r = httpx.get(f"{base}/v1/models",
                          headers={"Authorization": f"Bearer {key}"},
                          timeout=15.0, trust_env=False)
            if r.status_code == 200:
                self._cloud_status.setTextColor_(_OK)
                self._cloud_status.setStringValue_("✓ 连接成功，密钥有效")
            else:
                self._cloud_status.setTextColor_(_BAD)
                self._cloud_status.setStringValue_(
                    f"✗ HTTP {r.status_code}：密钥无效或额度不足")
        except Exception as e:
            self._cloud_status.setTextColor_(_BAD)
            self._cloud_status.setStringValue_(f"✗ 网络不通：{type(e).__name__}")

    def testLocal_(self, s):
        self._local_status.setTextColor_(_GRAY)
        self._local_status.setStringValue_("检测中…")
        if not shutil.which("ollama"):
            self._local_status.setTextColor_(_BAD)
            self._local_status.setStringValue_("✗ 未找到 ollama，请先 brew install ollama")
            return
        models = _ollama_models()
        want = self._f_omodel.stringValue().strip()
        if any(m == want or m.startswith(want) for m in models):
            self._local_status.setTextColor_(_OK)
            self._local_status.setStringValue_(f"✓ Ollama 运行中，{want} 已就位")
        elif models:
            self._local_status.setTextColor_(_BAD)
            self._local_status.setStringValue_(
                f"Ollama 在运行，但缺 {want}：ollama pull {want}")
        else:
            self._local_status.setTextColor_(_BAD)
            self._local_status.setStringValue_("✗ 无法读取模型列表（Ollama 服务未启动？）")

    def reloadModels_(self, s):
        self._f_omodel.removeAllItems()
        self._f_omodel.addItemsWithObjectValues_(_ollama_models())

    # ---------- Token 用量 ----------

    def _refresh_usage(self):
        from voice_input.usage import UsageLogger
        mem = self._cfg.get("memory", {})
        db = Path(mem.get("db", "data/memory.db")).expanduser()
        if not db.is_absolute():
            db = ROOT / db
        s = UsageLogger(db).summary()
        tp, tc = s["today"]
        ap, ac = s["total"]
        self._usage_today.setStringValue_(
            f"今日：输入 {tp:,} · 输出 {tc:,} · 合计 {tp + tc:,}")
        self._usage_total.setStringValue_(
            f"累计：输入 {ap:,} · 输出 {ac:,} · 合计 {ap + ac:,}")
        for i, lbl in enumerate(self._usage_feats):
            if i < len(s["by_feature"]):
                name, p, c = s["by_feature"][i]
                lbl.setStringValue_(f"{name}：{p + c:,}（入 {p:,} / 出 {c:,}）")
            else:
                lbl.setStringValue_("")

    def refreshUsage_(self, s):
        self._refresh_usage()

    # ---------- 保存 / 关闭 ----------

    def save_(self, s):
        try:
            _save_llm_config(
                provider=self._provider,
                base_url=self._f_base.stringValue().strip(),
                model=(self._f_model if self._provider == "openai"
                       else self._f_omodel).stringValue().strip(),
                keep_alive=self._f_keep.stringValue().strip(),
                api_key=self._f_key.stringValue().strip())
            self._save_status.setTextColor_(_OK)
            self._save_status.setStringValue_("✓ 已保存，重启 WaveType 后生效")
        except Exception as e:
            self._save_status.setTextColor_(_BAD)
            self._save_status.setStringValue_(f"✗ 保存失败：{type(e).__name__}: {e}")

    def closeWindow_(self, s):
        from AppKit import NSApp
        NSApp.terminate_(None)


def run() -> None:
    from AppKit import NSApplicationActivationPolicyRegular
    from PyObjCTools import AppHelper
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    SettingsController.alloc().init()
    app.activateIgnoringOtherApps_(True)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    sys.exit(run())
