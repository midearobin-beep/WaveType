"""纠错对提取（Ctrl+Fn 手动路径）：用户选中的"正确文本" vs 最近一次上屏文本，
定位"错误形式"。

CJK 同音纠错的特点是：长度几乎不变、共享字符很少（"知事"→"知识" 只共享"知"），
纯相似度指标会偏向超短窗口。策略：
1. 在 polished 里枚举 n-1 / n / n+1 / n+2 长度的窗口（n = 选区长度）
2. 淘汰"截断窗口"——窗口正文正好是选区的真前缀/后缀时，它只是选区的一部分，
   选它必然产出垃圾词条（实测旧实现把「…是 5 度」+「5℃」学成了 "5" → "5℃"，
   而 "5" 是 "5℃" 的前缀，属于典型截断）
3. 剩下的按（首尾字符是否对得上、覆盖率、长度接近度）打分取最优
4. 最后过一道 memory.is_learnable 质量闸门
"""
from __future__ import annotations

import difflib
import logging

from .memory import is_learnable

log = logging.getLogger(__name__)


def _match_count(a: str, b: str) -> int:
    return sum(m.size for m in difflib.SequenceMatcher(None, a, b).get_matching_blocks())


def extract_wrong(polished: str, selected: str, min_coverage: float = 0.3) -> str | None:
    """在 polished 里找被选区替换掉的片段。找不到/已正确则返回 None。"""
    if not polished or not selected:
        return None
    selected = selected.strip()
    if not selected:
        return None
    if selected in polished:
        return None  # 已经是对的，没有可学的
    n = len(selected)
    if n > len(polished):
        return None

    cands: list[tuple[int, float, int, str]] = []
    for wlen in {max(1, n - 1), n, n + 1, n + 2}:
        if wlen < 1 or wlen > len(polished):
            continue
        for i in range(0, len(polished) - wlen + 1):
            win = polished[i:i + wlen]
            body = win.strip()
            if not body:
                continue
            cov = _match_count(win, selected) / n
            if cov < min_coverage:
                continue
            if len(body) < n and (selected.startswith(body) or selected.endswith(body)):
                continue  # 截断窗口，见模块 docstring
            edge = (win[0] == selected[0]) + (win[-1] == selected[-1])
            cands.append((edge, cov, -abs(wlen - n), win))

    if not cands:
        log.info("未在上屏文本里定位到选区的对应片段：%r", selected)
        return None
    best = max(cands, key=lambda c: (c[0], c[1], c[2]))[3]
    wrong = best.strip()
    if not is_learnable(wrong, selected):
        log.info("候选片段未过质量闸门，不入库：%r → %r", wrong, selected)
        return None
    return wrong
