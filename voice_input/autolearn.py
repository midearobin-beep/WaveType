"""自动纠错学习（Typeless 式）：注入后监视目标输入框，用户改动自动入库。

机制：
1. 注入完成后锁定目标输入框（selection.FocusedText：元素只解析一次，全程复用）
2. 每 POLL_INTERVAL 秒读回内容；稳定（连续 STABLE_READS 次相同）且与注入文本不同
   → diff 提取改动对
3. 通过质量闸门的改动对写入词典，并即时回注（ASR 热词 + LLM 约束）
4. 终止：超时 / 下次听写 / 元素消失 / 前台 App 变了

**只学 replace（同位置替换），不学纯插入/删除**，这是刻意的：
   实测 difflib 对 10 类真实纠错的 opcode 分布——
     同音专名「巨龙→居隆」      replace（要做 CJK 边界扩展才能凑出整个词）
     符号单位「度→℃」「瓦→W」  replace（但带上前导空格，strip 后是单字对）
     单位「兆→MB」              replace
     英文重复词「the the the」  delete + insert（两者中间隔着 equal，无法安全合并）
     英文缺空格「datasheet」    insert 一个空格
   纯 insert/delete 无法表达成「错误形式 → 正确形式」这张表：
   「模块 → IPM 模块」这种插入对被当成全局替换规则会到处误伤，宁可不学。
   旧实现的真正漏洞是 `2 <= len(wrong)` 把单字符号对全挡了（度/瓦这类全丢），
   以及门槛之下没有任何质量闸门。

Ctrl+Fn 手动纠错保留为兜底（AX 读不出内容的场景）。
"""
from __future__ import annotations

import difflib
import logging
import threading
import time

from .memory import is_learnable
from .selection import FocusedText, frontmost_app_name

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.8     # 轮询间隔
WATCH_SECONDS = 60.0    # 监视窗口
STABLE_READS = 2        # 内容连续相同 N 次才判定"用户改完了"
CJK_EXPAND_MAX = 3      # CJK 同音纠错的边界回吸窗口上限
RESOLVE_RETRY_SECS = 8.0  # 注入后焦点未稳定（Electron/IM 常见）：此窗口内持续重试定位


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def _expand_boundaries(injected: str, current: str, i1: int, i2: int, j1: int, j2: int) -> tuple[int, int, int, int]:
    """边界吸附：弥补 difflib 对同音词或同词根英文的过度截断。
    1. CJK 边界向前吸附共享首字：
       针对 1~3 字短片段，若前一个字符是相同的 CJK 文字，向前吸附 1 个字形成词组
       （如 '共'→'泵' 吸附成 '热共'→'热泵'；'线'→'限' 吸附成 '下线'→'下限'；'巨龙'→'居隆' 吸附成 '万巨龙'→'万居隆'）。
       注意：不向后吸附，防止把"我的方案"→"你的方案"中句首的"我→你"吸成"我的→你的"进而绕过单字过滤。
    2. 拉丁字母边界：若局部改动两侧仍有相同字母（如 HICO→Heiko 切成 ICO→eiko），吸附回完整单词。
    """
    if 1 <= i2 - i1 <= CJK_EXPAND_MAX and i1 > 0 and j1 > 0:
        prev = injected[i1 - 1]
        right_head = current[j1:j1 + 1]
        if prev == current[j1 - 1] and _is_cjk(prev) and right_head and _is_cjk(right_head):
            i1 -= 1
            j1 -= 1

    # 拉丁字母完整词边界吸附
    while i1 > 0 and j1 > 0 and injected[i1 - 1] == current[j1 - 1] and injected[i1 - 1].isascii() and injected[i1 - 1].isalpha():
        i1 -= 1
        j1 -= 1
    while i2 < len(injected) and j2 < len(current) and injected[i2] == current[j2] and injected[i2].isascii() and injected[i2].isalpha():
        i2 += 1
        j2 += 1

    return i1, i2, j1, j2


def extract_pairs(injected: str, current: str) -> list[tuple[str, str]]:
    """从 (注入文本, 当前文本) 提取纠错对。

    只接受 replace 操作；单侧长度放宽到 1（符号/单位纠错），其余交给 is_learnable。
    """
    if not injected or not current or injected == current:
        return []
    sm = difflib.SequenceMatcher(None, injected, current, autojunk=False)
    # 注入文本必须在当前值里还有足够锚点（否则用户已切到无关内容）
    equal_chars = sum(m.size for m in sm.get_matching_blocks())
    if equal_chars < len(injected) * 0.4:
        return []

    pairs: list[tuple[str, str]] = []
    skipped: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag != "replace":
            skipped.append(f"{tag}({injected[i1:i2]!r}→{current[j1:j2]!r})")
            continue
        i1, i2, j1, j2 = _expand_boundaries(injected, current, i1, i2, j1, j2)
        wrong, right = injected[i1:i2].strip(), current[j1:j2].strip()
        if not is_learnable(wrong, right):
            skipped.append(f"噪音({wrong!r}→{right!r})")
            continue
        pairs.append((wrong, right))
    if skipped:
        # 不静默丢弃：这些是"看见了但决定不学"的改动，排障时要能对上
        log.debug("未入库的改动：%s", "，".join(skipped))
    return pairs


def summarize_diff(injected: str, current: str) -> str:
    """把 diff 压成一行人类可读描述，供日志使用（旧实现出问题完全无从查起）。"""
    if injected == current:
        return "无改动"
    sm = difflib.SequenceMatcher(None, injected, current, autojunk=False)
    bits = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        bits.append(f"{tag}:{injected[i1:i2]!r}→{current[j1:j2]!r}")
    return " ".join(bits) or "无改动"


class AutoLearner:
    def __init__(self, memory, on_learn, static_terms: list[str] | set[str] | None = None) -> None:
        self._memory = memory
        self._on_learn = on_learn  # 学到新词条后的回调（回注词典）
        self._static_terms_set = set(static_terms or [])  # 静态术语集合，用于高置信度判定
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def watch(self, injected: str, app: str = "", pid: int | None = None) -> None:
        """注入完成后调用：开始监视目标输入框。

        app/pid 用于校验"用户改动发生在同一个应用里"，避免跨应用误学
        （切窗口后读到的焦点输入框已经不是刚注入的那个了）。
        """
        self.stop()
        pid = pid if pid is not None else None
        with self._lock:
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(injected, app, pid, self._stop), daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            self._thread = None

    def _run(self, injected: str, app: str, pid, stop: threading.Event) -> None:
        target = FocusedText(pid)
        resolved = target.ok
        if resolved:
            log.info("自动学习监听中：%s（%s）", target.route, app or "未知应用")
        else:
            # 定位不到不等于放弃：注入刚完成时焦点/AX 树常未稳定（日志实测
            # 41% 的会话首次解析失败），在重试窗口内每轮轮询都重新解析。
            log.info("自动学习：暂未定位到目标输入框（%s），%.0f 秒内重试",
                     target.route, RESOLVE_RETRY_SECS)

        deadline = time.monotonic() + WATCH_SECONDS
        resolve_deadline = time.monotonic() + RESOLVE_RETRY_SECS
        last_value, stable = None, 0
        learned: set[tuple[str, str]] = set()
        edits_logged: set[str] = set()   # 同一稳定状态的风格编辑只记一次
        reason = "超时"
        while not stop.is_set() and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            if app and frontmost_app_name() != app:
                reason = f"前台应用已切走（{frontmost_app_name()}）"
                break
            current = target.value()
            if current is None:
                # 元素从未解析成功时 value() 每轮都会重试解析（FocusedText 内部逻辑）
                if not resolved and time.monotonic() < resolve_deadline:
                    continue
                reason = "目标输入框失效" if resolved else "始终未定位到输入框"
                break
            if not resolved:
                resolved = True
                log.info("自动学习重试定位成功：%s（%s）", target.route, app or "未知应用")
            if current == injected:
                continue  # 没人改
            if current == last_value:
                stable += 1
            else:
                stable = 0
                last_value = current
                continue
            if stable < STABLE_READS:
                continue
            # P1b 风格画像原料：不管改动能不能学成词条，整个"上屏 → 用户改后"
            # 的状态对先记进 style_edits（拆句/语气/格式类改动的唯一来源）。
            # 与词典学习独立：词典只收 replace 词条，风格编辑收全量。
            if current not in edits_logged and len(injected) >= 4:
                # 锚点校验与 extract_pairs 同标准（≥40% 相同字符），
                # 防止用户切到无关内容后把"别人的文本"记成编辑样本
                _sm = difflib.SequenceMatcher(None, injected, current, autojunk=False)
                _equal = sum(m.size for m in _sm.get_matching_blocks())
                if _equal >= len(injected) * 0.4:
                    edits_logged.add(current)
                    try:
                        self._memory.log_style_edit(injected, current, app)
                    except Exception:
                        log.debug("风格编辑记录失败", exc_info=True)
            pairs = extract_pairs(injected, current)
            if not pairs:
                log.debug("改动未入库：%s", summarize_diff(injected, current))
            for wrong, right in pairs:
                if (wrong, right) in learned:
                    continue
                learned.add((wrong, right))
                # 高置信度判定：若改正后的正确形式命中用户静态术语表，说明是确凿的专业词纠错，直接 trusted=True 即刻生效
                is_high_confidence = (right in self._static_terms_set)
                if not self._memory.add_correction(wrong, right, trusted=is_high_confidence):
                    log.info("自动学习被质量闸门拦下: %s → %s", wrong, right)
                    continue
                log.info("自动学习%s: %s → %s", "（高置信即刻生效）" if is_high_confidence else "", wrong, right)
                self._on_learn(wrong, right)
        if not resolved and reason == "超时":
            reason = f"重试 {RESOLVE_RETRY_SECS:.0f}s 后始终未定位到输入框"
        log.info("自动学习结束（%s，入库 %d 条）", reason, len(learned))
