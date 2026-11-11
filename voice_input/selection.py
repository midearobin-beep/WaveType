"""焦点文本读取：Accessibility API 多路兜底 + 剪贴板兜底（用完恢复原内容）。

两条用途：
1. 自动纠错学习（AutoLearner）轮询焦点输入框的完整内容 → FocusedText
2. Ctrl+Fn 手动纠错读选区 → read_selected_text

⚠️ 取数顺序由本机（macOS 27 beta）实测的 AX 行为决定，不要随意改回：
    系统级 AXUIElementCreateSystemWide() + kAXFocusedUIElementAttribute
      → 恒返回 -25204 kAXErrorCannotComplete（连续 5/5 次；kAXFocusedApplicationAttribute
        同样失败），长 messaging timeout 也救不回来
    应用级 AXUIElementCreateApplication(pid) + kAXFocusedUIElementAttribute
      → 正常，能读到 Electron/Chromium 文本框的 AXValue
    同一进程 AXIsProcessTrusted() == True，且跨进程读别的 App 的 AXValue 成功
      → 所以这不是权限问题，是**系统级入口本身在这台机器上不可用**。
    旧实现只用系统级入口，于是 read_focused_value() 永远返回 None，
    自动学习线程第一轮轮询就退出——这是词库始终为空的根因。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import Quartz
from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementSetMessagingTimeout,
    AXValueGetValue,
    kAXChildrenAttribute,
    kAXFocusedUIElementAttribute,
    kAXFocusedWindowAttribute,
    kAXSelectedTextAttribute,
    kAXSelectedTextRangeAttribute,
    kAXValueCFRangeType,
)

log = logging.getLogger(__name__)

# AX 只回一个负数错误码，不知道含义就没法排障
_AX_ERR = {
    0: "OK",
    -25200: "Failure",
    -25201: "IllegalArgument",
    -25202: "InvalidUIElement",
    -25203: "InvalidUIElementObserver",
    -25204: "CannotComplete",
    -25205: "AttributeUnsupported",
    -25207: "NotificationUnsupported",
    -25211: "APIDisabled",
    -25212: "NoValue",
}


def ax_err(code: int) -> str:
    return _AX_ERR.get(code, f"err{code}")


MESSAGING_TIMEOUT = 1.0   # 单次 AX 调用上限：目标 App 卡死时不能拖垮监听线程
MAX_VALUE_CHARS = 50_000  # 超长文档截断（diff 成本保护）
TEXT_ROLES = {"AXTextArea", "AXTextField", "AXComboBox", "AXSearchField"}
MAX_WALK_DEPTH = 16       # Electron/Chromium 的输入框埋在 AXWebArea 之下好几层
MAX_WALK_NODES = 600      # AX 树懒加载，遍历必须有上限（放宽以覆盖 IM/Electron 深树）


# ---------- 基础封装 ----------

def _attr(elem, name):
    """读一个 AX 属性，返回 (值, 错误码)。任何异常都收敛成错误码。"""
    try:
        err, value = AXUIElementCopyAttributeValue(elem, name, None)
    except Exception as exc:  # PyObjC 偶尔抛，不让它冒泡
        log.debug("AX 属性读取异常 %s: %s", name, exc)
        return None, -25200
    return value, err


def _value_of(elem) -> str | None:
    """元素非空的 AXValue；空/无此属性都返回 None。"""
    value, _ = _attr(elem, "AXValue")
    if isinstance(value, str) and value.strip():
        return value[:MAX_VALUE_CHARS]
    return None


def _role_of(elem) -> str:
    role, _ = _attr(elem, "AXRole")
    return str(role) if role else "?"


def frontmost_pid() -> int | None:
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(app.processIdentifier()) if app else None
    except Exception:
        return None


def frontmost_app_name() -> str:
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return app.localizedName() if app else ""
    except Exception:
        return ""


def frontmost_window_title(pid: int | None = None) -> str:
    """读取当前前台/焦点窗口的标题（如聊天对象名、网页标题、文档文件名）。"""
    if pid is None:
        pid = frontmost_pid()
    if pid is None:
        return ""
    root, err = _focused_window(pid)
    if root is not None:
        title, _ = _attr(root, "AXTitle")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return ""


def _app_element(pid: int):
    elem = AXUIElementCreateApplication(pid)
    try:
        AXUIElementSetMessagingTimeout(elem, MESSAGING_TIMEOUT)
    except Exception:
        pass
    return elem


def _system_element():
    elem = AXUIElementCreateSystemWide()
    try:
        AXUIElementSetMessagingTimeout(elem, MESSAGING_TIMEOUT)
    except Exception:
        pass
    return elem


def focused_element(pid: int | None = None):
    """定位当前有焦点的 UI 元素，返回 (元素|None, 来源说明)。

    优先应用级（见模块 docstring 的实测依据），系统级只作兜底。
    """
    if pid is not None:
        elem, err = _attr(_app_element(pid), kAXFocusedUIElementAttribute)
        if elem is not None:
            return elem, f"应用级焦点(pid={pid})"
        log.debug("应用级焦点元素不可用：%s", ax_err(err))
    elem, err = _attr(_system_element(), kAXFocusedUIElementAttribute)
    if elem is not None:
        return elem, "系统级焦点"
    log.debug("系统级焦点元素不可用：%s（本机已知问题）", ax_err(err))
    return None, f"无焦点元素({ax_err(err)})"


def _focused_window(pid: int | None):
    """焦点窗口，用于在焦点元素不是文本框时向下搜索。"""
    roots = []
    if pid is not None:
        roots.append(_app_element(pid))
    roots.append(_system_element())
    last = 0
    for root in roots:
        win, err = _attr(root, kAXFocusedWindowAttribute)
        if win is not None:
            return win, 0
        last = err
    return None, last


def _walk_text_fields(root) -> list[tuple[str, str, object]]:
    """广度优先收集子树里非空的文本框，返回 [(角色, 值, 元素)]。

    上限：MAX_WALK_DEPTH 层 / MAX_WALK_NODES 个节点 / 每层最多 60 个子节点。
    """
    hits: list[tuple[str, str, object]] = []
    queue = [(root, 0)]
    nodes = 0
    while queue and nodes < MAX_WALK_NODES:
        elem, depth = queue.pop(0)
        nodes += 1
        role = _role_of(elem)
        if role in TEXT_ROLES:
            value = _value_of(elem)
            if value:
                hits.append((role, value, elem))
        if depth >= MAX_WALK_DEPTH:
            continue
        kids, err = _attr(elem, kAXChildrenAttribute)
        if err == 0 and kids:
            for kid in list(kids)[:60]:
                queue.append((kid, depth + 1))
    return hits


# ---------- 自动学习用：锁定目标输入框 ----------

class FocusedText:
    """一次听写期间锁定的目标输入框：元素解析一次，全程复用。

    value() 只在元素失效（AX 报错）时才重新解析——Electron 会重建输入框元素，
    但把「字段本来就是空的」当成失效会导致每轮轮询都重解析（白烧 CPU）。
    """

    def __init__(self, pid: int | None = None) -> None:
        self._pid = pid
        self.element = None
        self.route = "未解析"
        self._resolve()

    def _resolve(self) -> None:
        elem, route = focused_element(self._pid)
        if elem is not None:
            if _value_of(elem) is not None:
                self.element, self.route = elem, route
                return
            route = f"{route}但角色 {_role_of(elem)} 无内容"
        root, werr = _focused_window(self._pid)
        if root is None:
            self.route = f"{route}，且无焦点窗口({ax_err(werr)})"
            return
        hits = _walk_text_fields(root)
        if len(hits) == 1:
            self.element, self.route = hits[0][2], f"{route} → 树搜索唯一命中 {hits[0][0]}"
            return
        areas = [h for h in hits if h[0] == "AXTextArea"]
        if len(areas) == 1:
            self.element, self.route = areas[0][2], f"{route} → 树搜索唯一多行框"
            return
        self.route = f"{route} → 树搜索命中 {len(hits)} 个文本框，无法唯一定位"

    @property
    def ok(self) -> bool:
        return self.element is not None

    def value(self) -> str | None:
        """当前内容。返回 None 表示元素失效（已尝试重解析仍未成功）。"""
        if self.element is not None:
            try:
                err, raw = AXUIElementCopyAttributeValue(self.element, "AXValue", None)
            except Exception:
                err, raw = -25200, None
            if err == 0:
                if isinstance(raw, str):
                    return raw[:MAX_VALUE_CHARS]
                return ""  # 元素还在，只是内容空/无该属性
            log.debug("目标元素读取失败(%s)，重新解析", ax_err(err))
            self.element = None
        self._resolve()
        return _value_of(self.element) if self.element is not None else None

    def selected_text(self) -> str | None:
        """元素内的选中文本（Ctrl+Fn 手动纠错用）。"""
        if self.element is None:
            return None
        sel, err = _attr(self.element, kAXSelectedTextAttribute)
        if isinstance(sel, str) and sel.strip():
            return str(sel)
        log.debug("元素无选区：%s", ax_err(err))
        return None


def resolve_focused_text(pid: int | None = None) -> FocusedText:
    return FocusedText(pid)


def read_focused_value(pid: int | None = None) -> str | None:
    """读焦点输入框的完整内容（每次重新解析，测试/一次性调用用）。"""
    target = FocusedText(pid)
    if not target.ok:
        log.debug("未定位到目标输入框：%s", target.route)
        return None
    return target.value()


# ---------- Ctrl+Fn 用：读选区 ----------

def _read_ax(pid: int | None = None) -> str | None:
    elem, route = focused_element(pid)
    if elem is None:
        log.debug("选区读取失败：%s", route)
        return None
    sel, err = _attr(elem, kAXSelectedTextAttribute)
    if isinstance(sel, str) and sel.strip():
        return str(sel)
    log.debug("AX 无选区（%s / %s）", route, ax_err(err))
    return None


def _read_clipboard() -> str | None:
    """模拟 Cmd+C 读取选区，并尽可能恢复原剪贴板各项内容。"""
    from AppKit import NSPasteboard

    pb = NSPasteboard.generalPasteboard()
    # 保留所有剪贴板类型，并用 changeCount 判断复制是否发生；无需先清空。
    items = []
    for item in pb.pasteboardItems() or []:
        items.append({kind: item.dataForType_(kind) for kind in item.types()})
    before = pb.changeCount()
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for key, down, flags in ((0x37, True, 0), (0x08, True, Quartz.kCGEventFlagMaskCommand),
                             (0x08, False, Quartz.kCGEventFlagMaskCommand), (0x37, False, 0)):
        ev = Quartz.CGEventCreateKeyboardEvent(src, key, down)
        if flags:
            Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    time.sleep(0.15)
    copied_count = pb.changeCount()
    if copied_count == before:
        return None
    new = pb.stringForType_("public.plain-text")
    # 如果其他程序又更新了剪贴板，不覆盖其新内容。
    if pb.changeCount() == copied_count:
        from AppKit import NSPasteboardItem
        restored = []
        for representations in items:
            item = NSPasteboardItem.alloc().init()
            for kind, data in representations.items():
                if data is not None:
                    item.setData_forType_(data, kind)
            restored.append(item)
        pb.clearContents()
        if restored:
            pb.writeObjects_(restored)
    return str(new) if new else None


def read_selected_text(pid: int | None = None) -> str | None:
    """读当前选区文本；无选区或失败返回 None。"""
    text = _read_ax(pid)
    if text:
        log.info("选区(AX): %s", text[:50])
        return text
    text = _read_clipboard()
    if text:
        log.info("选区(剪贴板): %s", text[:50])
        return text
    log.info("未读到选区（AX 与剪贴板均失败）")
    return None


# ---------- P0：光标上下文（对标 Typeless cursor_state） ----------

BEFORE_CHARS = 300      # 光标前截取上限（Typeless 同款思路：够衔接上文即可）
AFTER_CHARS = 120       # 光标后截取上限


def _selected_range(elem) -> tuple[int, int] | None:
    """读元素的 AXSelectedTextRange，返回 (location, length)；不支持返回 None。"""
    axval, err = _attr(elem, kAXSelectedTextRangeAttribute)
    if axval is None:
        log.debug("无 AXSelectedTextRange：%s", ax_err(err))
        return None
    try:
        ok, rng = AXValueGetValue(axval, kAXValueCFRangeType, None)
    except Exception as exc:
        log.debug("SelectedTextRange 解码异常：%s", exc)
        return None
    if not ok or rng is None:
        return None
    try:
        return int(rng.location), int(rng.length)
    except AttributeError:  # 部分 pyobjc 版本返回 tuple
        try:
            return int(rng[0]), int(rng[1])
        except Exception:
            return None


@dataclass
class InputTarget:
    pid: int
    app: str
    element: object
    selection_range: tuple[int, int] | None
    value: str | None
    window: object | None = None
    window_title: str = ""


def capture_input_target() -> InputTarget | None:
    """记录当前真实焦点，不用树搜索猜测输入框。"""
    pid = frontmost_pid()
    if pid is None:
        return None
    w_title = frontmost_window_title(pid)
    element, _ = focused_element(pid)
    if element is None:
        window, _ = _focused_window(pid)
        if window is None or frontmost_pid() != pid:
            log.info("无法确认输入焦点或窗口，保留结果供手动复制")
            return None
        log.info("无法读取焦点元素，使用前台窗口校验（pid=%s）", pid)
        return InputTarget(pid, frontmost_app_name(), None, None, None, window, window_title=w_title)
    if frontmost_pid() != pid:
        return None
    value, _ = _attr(element, "AXValue")
    return InputTarget(pid, frontmost_app_name(), element, _selected_range(element),
                       value if isinstance(value, str) else None, window_title=w_title)


def input_target_matches(target: InputTarget | None, *, check_content: bool = True,
                         inserted: str = "") -> bool:
    """校验真实焦点及可读取的内容；缺失的 AX 属性不视为焦点变化。"""
    if target is None or frontmost_pid() != target.pid:
        return False
    if target.element is None:
        if target.window is None:
            return False
        window, _ = _focused_window(target.pid)
        return window is not None and window == target.window and frontmost_pid() == target.pid
    current, _ = focused_element(target.pid)
    if current is None or current != target.element:
        return False
    if not check_content:
        return True
    value, _ = _attr(current, "AXValue")
    current_range = _selected_range(current)
    if not inserted:
        return ((target.value is None or value == target.value)
                and (target.selection_range is None or current_range == target.selection_range))
    if target.selection_range is None:
        # 无法推算输入后的文本或光标，只能校验焦点。
        return True
    location, length = target.selection_range
    end = location + len(inserted.encode("utf-16-le")) // 2
    if current_range != (end, 0):
        return False
    if target.value is None:
        return True
    data = target.value.encode("utf-16-le")
    if location < 0 or length < 0 or (location + length) * 2 > len(data):
        return False
    try:
        expected = (data[:location * 2] + inserted.encode("utf-16-le")
                    + data[(location + length) * 2:]).decode("utf-16-le")
    except UnicodeDecodeError:
        return False
    return value == expected


def target_selected_text(target: InputTarget | None) -> str | None:
    """AX 选区以 UTF-16 为单位；不能可靠解码时不允许替换。"""
    if target is None or target.value is None or target.selection_range is None:
        return None
    location, length = target.selection_range
    if location < 0 or length <= 0:
        return None
    data = target.value.encode("utf-16-le")
    if (location + length) * 2 > len(data):
        return None
    try:
        return data[location * 2:(location + length) * 2].decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def read_cursor_context(pid: int | None = None) -> dict:
    """读焦点输入框的光标上下文：{"before", "after", "selected", "role"}。

    before = 光标前最多 BEFORE_CHARS 字，after = 光标后最多 AFTER_CHARS 字。
    用途：润色时注入 <上文>/<下文>，让输出与已有内容自然衔接
    （不重复称呼落款、接着列表编号）。任一环节失败 → 字段为空串，绝不抛异常：
    上下文是增强不是刚需，读不到就退化为旧行为。
    """
    ctx = {"before": "", "after": "", "selected": "", "role": ""}
    elem, route = focused_element(pid)
    if elem is None:
        log.debug("光标上下文读取失败：%s", route)
        return ctx
    ctx["role"] = _role_of(elem)
    value, err = _attr(elem, "AXValue")
    if not isinstance(value, str):
        log.debug("光标上下文无 AXValue（%s / %s）", route, ax_err(err))
        return ctx
    value = value[:MAX_VALUE_CHARS]
    rng = _selected_range(elem)
    if rng is None:
        # 读不到光标位置时无法拆分前后文，宁缺毋滥（整框塞进"上文"会
        # 误导模型把全文当成需要接续的内容）
        return ctx
    loc, length = rng
    data = value.encode("utf-16-le")
    if loc < 0 or length < 0 or (loc + length) * 2 > len(data):
        return ctx
    try:
        ctx["before"] = data[:loc * 2].decode("utf-16-le")[-BEFORE_CHARS:]
        ctx["selected"] = data[loc * 2:(loc + length) * 2].decode("utf-16-le")
        ctx["after"] = data[(loc + length) * 2:].decode("utf-16-le")[:AFTER_CHARS]
    except UnicodeDecodeError:
        return {"before": "", "after": "", "selected": "", "role": ctx["role"]}
    log.debug("光标上下文：前 %d 字 / 选 %d 字 / 后 %d 字",
              len(ctx["before"]), length, len(ctx["after"]))
    return ctx
