"""自动纠错学习（Typeless 式）：注入后监视目标输入框，用户改动自动入库。

机制：
1. 注入完成后，记录注入文本，并开始轮询焦点输入框的 AXValue（每 0.8s）
2. 读回值稳定（连续两次相同）且与注入文本不同 → diff 提取改动对
3. 符合条件的改动对自动写入词典并即时回注
4. 终止条件：超时 / 用户开始下一次听写 / 焦点元素消失

Ctrl+Fn 手动纠错保留为兜底（AX 读不出内容的 Electron 应用等场景）。
"""
from __future__ import annotations

import difflib
import logging
import threading
import time

from .selection import read_focused_value

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.8     # 轮询间隔
WATCH_SECONDS = 60.0    # 监视窗口
STABLE_READS = 2        # 内容连续相同 N 次才判定"用户改完了"


def extract_pairs(injected: str, current: str) -> list[tuple[str, str]]:
    """从 (注入文本, 当前文本) 提取纠错对。

    只接受 replace 操作（删一段加一段），过滤过短/比例失衡的噪音改动。
    """
    if not injected or not current or injected == current:
        return []
    # 注入文本必须在当前值里还有足够锚点（否则用户已切到无关内容）
    sm = difflib.SequenceMatcher(None, injected, current, autojunk=False)
    equal_chars = sum(m.size for m in sm.get_matching_blocks())
    if equal_chars < len(injected) * 0.4:
        return []
    import re
    pairs = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace":
            continue
        # 边界扩展（仅一步、仅 CJK 同音纠错场景）：difflib 会把共享的首字锚定到
        # replace 区间外（"万巨龙"→"万居隆" 只报 "巨龙"→"居隆"）。对 2–3 字且
        # 右侧也是 CJK 的短片段，把前一个相同 CJK 字吸回来，词条更具体，
        # 避免泛化误伤（如"巨龙大厦"）。单字对（明→后）不扩展，且被长度过滤掉。
        wlen_raw = i2 - i1
        right_raw = current[j1:j2]
        if (2 <= wlen_raw <= 3 and i1 > 0 and j1 > 0
                and injected[i1 - 1] == current[j1 - 1]
                and '一' <= injected[i1 - 1] <= '鿿'
                and right_raw and '一' <= right_raw[0] <= '鿿'):
            i1 -= 1
            j1 -= 1
        wrong, right = injected[i1:i2].strip(), current[j1:j2].strip()
        # 过滤：太短无意义、比例失衡（通常是删改段落而非改词）、纯标点变化
        if not (2 <= len(wrong) <= 20 and 2 <= len(right) <= 20):
            continue
        if wrong == right:
            continue
        if not (0.3 <= len(wrong) / len(right) <= 3.0):
            continue
        # 纯标点/空白变化不学：剥掉所有非标文字符后相同则跳过
        strip = lambda s: re.sub(r"[^\w一-鿿]", "", s)
        if strip(wrong) == strip(right):
            continue
        pairs.append((wrong, right))
    return pairs


class AutoLearner:
    def __init__(self, memory, on_learn) -> None:
        self._memory = memory
        self._on_learn = on_learn  # 学到新词条后的回调（回注词典）
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def watch(self, injected: str) -> None:
        """注入完成后调用：开始监视目标输入框。"""
        self.stop()
        with self._lock:
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(injected, self._stop), daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            self._thread = None

    def _run(self, injected: str, stop: threading.Event) -> None:
        deadline = time.monotonic() + WATCH_SECONDS
        last_value, stable = None, 0
        learned: set[tuple[str, str]] = set()
        while not stop.is_set() and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            current = read_focused_value()
            if current is None:
                break  # 焦点元素消失（切窗口/关文档）
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
            for wrong, right in extract_pairs(injected, current):
                if (wrong, right) in learned:
                    continue
                learned.add((wrong, right))
                self._memory.add_correction(wrong, right)
                log.info("自动学习: %s → %s", wrong, right)
                print(f"🧠 自动学习：{wrong} → {right}")
                self._on_learn()
