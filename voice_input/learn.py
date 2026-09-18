"""纠错对提取：用户选中的"正确文本" vs 最近一次上屏文本，定位"错误形式"。

CJK 同音纠错的特点是：长度几乎不变、共享字符很少（"知事"→"知识" 只共享"知"），
纯相似度指标会偏向超短窗口。策略：
1. 首选：等长窗口中首字符与选区相同的（同音替换最典型的形态）
2. 兜底：±1 长窗口按覆盖率排序（覆盖率 = 匹配字符数 / len(选区)）
"""
from __future__ import annotations

import difflib


def _match_count(a: str, b: str) -> int:
    return sum(m.size for m in difflib.SequenceMatcher(None, a, b).get_matching_blocks())


def extract_wrong(polished: str, selected: str, min_coverage: float = 0.3) -> str | None:
    """在 polished 里找被选区替换掉的片段。找不到/已正确则返回 None。"""
    if not polished or not selected:
        return None
    selected = selected.strip()
    if selected in polished:
        return None  # 已经是对的，没有可学的
    n = len(selected)
    if n < 2 or n > len(polished):
        return None

    def windows(wlen: int):
        if 2 <= wlen <= len(polished):
            for i in range(0, len(polished) - wlen + 1):
                win = polished[i:i + wlen]
                cov = _match_count(win, selected) / n
                if cov >= min_coverage:
                    yield win, cov

    # 首选：等长 + 首字相同
    same_start = [(win, cov) for win, cov in windows(n) if win[0] == selected[0]]
    if same_start:
        return max(same_start, key=lambda x: x[1])[0]
    # 兜底：±1 窗口，覆盖率优先，其次首/尾字符一致
    cands = []
    for wlen in (n - 1, n + 1, n):
        for win, cov in windows(wlen):
            edge = (win[0] == selected[0]) + (win[-1] == selected[-1])
            cands.append((cov, edge, -abs(wlen - n), win))
    return max(cands)[3] if cands else None
