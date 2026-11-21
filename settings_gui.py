#!/usr/bin/env python3
"""WaveType 设置界面（PyObjC 原生 GUI）。

一块窗口搞定日常设置：
- 润色模型：云端 OpenAI 兼容 API 或本地 Ollama
- 翻译输出：可选择常用语言或输入语言名称，默认俄语
- Ask 联网搜索：仅 DeepSeek Flash 云端模型可用
- Token 用量：今日 / 累计 / 按功能（润色、翻译、问答）分项统计

保存写回 config.yaml 的 llm、translation 与 Ask 联网搜索设置（精准替换，保留其余内容与注释）；
API key 只写 .env，绝不进 config.yaml。

运行：python main.py --settings   或   python settings_gui.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

import objc
import yaml
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

from voice_input.polish import TRANSLATE_LANGUAGE_LABELS, translation_target

ROOT = Path(__file__).resolve().parent
WIN_W, WIN_H = 560.0, 800.0

_GRAY = NSColor.colorWithCalibratedWhite_alpha_(0.55, 1.0)
_TEXT = NSColor.labelColor()


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


def _load_config(cfg_path: Path) -> dict:
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


def _write_config(cfg: Path, content: str) -> None:
    # 先写临时文件，再原子替换，避免翻译时读到只写了一部分的配置。
    cfg = cfg.resolve(strict=True)
    mode = cfg.stat().st_mode & 0o777
    with NamedTemporaryFile(dir=cfg.parent, mode="w", encoding="utf-8", delete=False) as file:
        temp = Path(file.name)
        try:
            file.write(content)
            file.close()
            temp.chmod(mode)
            temp.replace(cfg)
        finally:
            temp.unlink(missing_ok=True)


def _save_llm_config(cfg: Path, provider: str, base_url: str, model: str,
                     keep_alive: str, api_key: str) -> None:
    """精准替换 config.yaml 的 llm 段活动行（注释与其余段落原样保留）。"""
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
    _write_config(cfg, "\n".join(lines) + "\n")

    if provider == "openai" and api_key and api_key != _load_env_key():
        (ROOT / ".env").write_text(f"DEEPSEEK_API_KEY={api_key}\n", encoding="utf-8")


def _save_translation_config(cfg: Path, target_language: str) -> None:
    """精准写回 translation.target_language，保留配置文件的其他内容与注释。"""
    target_language = target_language.strip()
    if not target_language:
        raise ValueError("请输入翻译目标语言")
    cfg = cfg.resolve(strict=True)
    original = cfg.read_text(encoding="utf-8")
    expected = yaml.safe_load(original)
    doc = yaml.compose(original)
    if not isinstance(doc, yaml.MappingNode) or doc.flow_style:
        raise ValueError("配置文件应使用分行的 YAML 键值格式")

    values = expected.get("translation")
    options = (values if isinstance(values, dict) else {}) | {"target_language": target_language}
    expected["translation"] = options
    section = next((v for k, v in doc.value if k.value == "translation"), None)
    scalar = json.dumps(target_language, ensure_ascii=False)
    field = f"target_language: {scalar}"
    if section is None:
        start = end = doc.end_mark.index
        prefix = "" if original[:start].endswith("\n") else "\n"
        indent = " " * doc.start_mark.column
        replacement = f"{prefix}\n{indent}translation:\n{indent}  {field}\n"
    elif isinstance(section, yaml.MappingNode) and not section.flow_style:
        target = next((v for k, v in section.value if k.value == "target_language"), None)
        if target is None:
            mark = section.start_mark
            start = end = mark.index
            replacement = f"{field}\n{' ' * mark.column}"
        else:
            start, end = target.start_mark.index, target.end_mark.index
            replacement = f" {scalar}" if start == end else scalar
            if target.end_mark.line > target.start_mark.line:
                replacement += "\n"
    else:
        start, end = section.start_mark.index, section.end_mark.index
        replacement = yaml.safe_dump(options, default_flow_style=True, width=float("inf")).strip()
        if start == end:
            replacement = " " + replacement

    updated = original[:start] + replacement + original[end:]
    if yaml.safe_load(updated) != expected:
        raise ValueError("无法只修改翻译语言，请检查 translation 配置格式")
    if updated != original:
        _write_config(cfg, updated)


def _ask_web_search_enabled(cfg: dict) -> bool:
    """只接受 YAML 的布尔值，旧配置或无效值均安全地关闭。"""
    return cfg.get("ask_web_search") is True


def _save_ask_web_search_config(cfg: Path, enabled: bool) -> None:
    """精准写回顶层 ask_web_search，保留其他配置和注释。"""
    if type(enabled) is not bool:
        raise ValueError("联网搜索设置必须为开或关")
    cfg = cfg.resolve(strict=True)
    original = cfg.read_text(encoding="utf-8")
    expected = yaml.safe_load(original)
    doc = yaml.compose(original)
    if not isinstance(doc, yaml.MappingNode) or doc.flow_style:
        raise ValueError("配置文件应使用分行的 YAML 键值格式")
    if not isinstance(expected, dict):
        raise ValueError("配置文件应为 YAML 映射")

    expected["ask_web_search"] = enabled
    value = next((node for key, node in doc.value if key.value == "ask_web_search"), None)
    scalar = "true" if enabled else "false"
    if value is None:
        start = end = doc.end_mark.index
        prefix = "" if original[:start].endswith("\n") else "\n"
        indent = " " * doc.start_mark.column
        replacement = f"{prefix}\n{indent}ask_web_search: {scalar}\n"
    else:
        start, end = value.start_mark.index, value.end_mark.index
        replacement = f" {scalar}" if start == end else scalar
        if value.end_mark.line > value.start_mark.line:
            replacement += "\n"
    updated = original[:start] + replacement + original[end:]
    if yaml.safe_load(updated) != expected:
        raise ValueError("无法只修改 Ask 联网搜索设置，请检查配置格式")
    if updated != original:
        _write_config(cfg, updated)


class SettingsController(NSObject):
    def initWithConfigPath_(self, config_path):
        self = objc.super(SettingsController, self).init()
        if self is None:
            return None
        self._config_path = Path(config_path)
        cfg = _load_config(self._config_path)
        llm = cfg.get("llm", {})
        self._provider = llm.get("provider", "openai")
        self._cfg = cfg
        self._build_window()
        self._load_values(llm)
        self._saved_model_settings = self._model_settings()
        self._ask_web_search = _ask_web_search_enabled(cfg)
        self._f_ask_web_search.setState_(1 if self._ask_web_search else 0)
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
        self._win.setReleasedWhenClosed_(False)
        self._win.setDelegate_(self)
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
        self._local_box.addSubview_(_label("keep_alive", 0, 108, 90, 20, size=12.0, color=_GRAY))
        self._f_keep = _field("", 92, 106, 120)
        self._local_box.addSubview_(self._f_keep)
        self._local_box.addSubview_(_label(
            "模型驻留时长，避免闲置卸载后首句重新加载（约 30s）。",
            222, 108, cw - 222, 20, size=11.0, color=_GRAY))
        self._local_status = _label("", 0, 26, cw, 34, size=12.0, wrap=True)
        self._local_box.addSubview_(self._local_status)
        c.addSubview_(self._local_box)

        # ---- 翻译输出语言 ----
        fy = WIN_H - 300
        self._build_translation_section(c, fy, w)

        # ---- Ask 联网搜索 ----
        sy = fy - 112
        c.addSubview_(_label("Ask 联网搜索", 24, sy, 200, 20, size=14.0, bold=True))
        self._f_ask_web_search = NSButton.alloc().initWithFrame_(NSMakeRect(24, sy - 34, 260, 24))
        self._f_ask_web_search.setButtonType_(3)
        self._f_ask_web_search.setTitle_("允许 Ask 按需联网搜索")
        self._f_ask_web_search.setAccessibilityLabel_("Ask 联网搜索")
        c.addSubview_(self._f_ask_web_search)
        c.addSubview_(_label(
            "仅 DeepSeek 的 deepseek-flash 云端模型可用。搜索由模型决定，搜索本身不单独收费，"
            "网页内容产生的模型 Token 会计费。保存后下一次 Ask 生效。",
            24, sy - 74, w, 42, size=11.0, color=_GRAY, wrap=True))

        # ---- Token 用量区 ----
        ty = sy - 108
        c.addSubview_(_label("Token 用量", 24, ty, 200, 20, size=14.0, bold=True))
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
            24, ty - 166, w, 32, size=11.0, color=_GRAY, wrap=True))

        self._build_buttons(c)

    def _build_translation_section(self, view, y, width):
        view.addSubview_(_label("翻译", 24, y, 200, 20, size=14.0, bold=True))
        view.addSubview_(_label("输出语言", 24, y - 34, 90, 20, size=12.0, color=_GRAY))
        self._f_target = NSComboBox.alloc().initWithFrame_(NSMakeRect(116, y - 38, 260, 28))
        self._f_target.setEditable_(True)
        self._f_target.setAccessibilityLabel_("输出语言")
        self._f_target.setNumberOfVisibleItems_(10)
        self._translation_target = translation_target(self._cfg)
        self._f_target.addItemsWithObjectValues_(list(TRANSLATE_LANGUAGE_LABELS.values()))
        self._f_target.setStringValue_(TRANSLATE_LANGUAGE_LABELS.get(
            self._translation_target, self._translation_target))
        view.addSubview_(self._f_target)
        view.addSubview_(_label(
            "可从下拉列表选择，也可直接输入语言名。保存后的下一次翻译会使用所选语言。",
            24, y - 82, width, 36, size=11.0, color=_GRAY, wrap=True))

    def _build_buttons(self, c):
        c.addSubview_(_button("保存并关闭", WIN_W - 140, 18, 116, 30, self, "save:", primary=True))

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

    # ---------- Token 用量 ----------

    def _refresh_usage(self):
        from voice_input.usage import UsageLogger
        mem = self._cfg.get("memory", {})
        db = Path(mem.get("db", "data/memory.db")).expanduser()
        if not db.is_absolute():
            db = self._config_path.parent / db
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

    # ---------- 保存 / 关闭 ----------

    def _model_settings(self) -> dict[str, str]:
        model = self._f_model if self._provider == "openai" else self._f_omodel
        return dict(provider=self._provider,
                    base_url=self._f_base.stringValue().strip(),
                    model=model.stringValue().strip(),
                    keep_alive=self._f_keep.stringValue().strip(),
                    api_key=self._f_key.stringValue().strip())

    def save_(self, s):
        try:
            self._win.endEditingFor_(None)
            language = self._f_target.stringValue().strip()
            if not language:
                raise ValueError("请输入翻译目标语言")
            target = next((value for value, label in TRANSLATE_LANGUAGE_LABELS.items()
                           if language == label), language)
            ask_web_search = bool(self._f_ask_web_search.state())
            model_settings = self._model_settings()
            if model_settings != self._saved_model_settings:
                _save_llm_config(self._config_path, **model_settings)
                self._saved_model_settings = model_settings
            if target != self._translation_target:
                _save_translation_config(self._config_path, target)
                self._translation_target = target
            if ask_web_search != self._ask_web_search:
                _save_ask_web_search_config(self._config_path, ask_web_search)
                self._ask_web_search = ask_web_search
            self._win.performClose_(None)
        except Exception as e:
            self._show_save_error(str(e))

    def _show_save_error(self, message: str) -> None:
        from AppKit import NSAlert, NSAlertStyleCritical
        alert = NSAlert.alloc().init()
        alert.setAlertStyle_(NSAlertStyleCritical)
        alert.setMessageText_("设置未保存")
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("好")
        alert.beginSheetModalForWindow_completionHandler_(self._win, None)

    def windowWillClose_(self, notification):
        from AppKit import NSApp
        NSApp.terminate_(None)


def run(config_path: Path = ROOT / "config.yaml") -> None:
    from AppKit import NSApplicationActivationPolicyRegular
    from PyObjCTools import AppHelper
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    controller = SettingsController.alloc().initWithConfigPath_(str(config_path.expanduser()))
    controller._win.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    sys.exit(run())
