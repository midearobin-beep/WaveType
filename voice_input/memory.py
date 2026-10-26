"""记忆层：SQLite 个人词典 + 历史记录。

飞轮设计（对标 Typeless Personal Dictionary，但数据完全本地）：
- dictionary：纠错对（wrong → right），count 累计命中次数
- history：每次听写的 raw ASR / 润色结果 / 前台 App，供后续风格画像使用
- 词典回注两条路：ASR context 热词（源头防错）+ 润色 prompt 映射（兜底纠错）

**质量闸门放在这一层**（is_learnable）：词典会以「以下写法必须严格遵守」的形式
进润色 prompt，一条垃圾词条会把全文都改错（旧代码实测存过 "5" → "5℃"）。
自动路径（自动学习 / Ctrl+Fn）必须先过闸门；`dict add` 是用户显式意图，放行。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dictionary (
    wrong TEXT NOT NULL,
    right TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (wrong, right)
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    app TEXT,
    raw TEXT NOT NULL,
    polished TEXT NOT NULL
);
-- P1b 风格画像（对标 Typeless /user/traits，数据完全本地）：
-- style_edits：用户对上屏文本的每次编辑原样（injected → edited），是风格信号原料
-- style_traits：提炼产物，只存抽象偏好描述（"聊天类偏好短句"），不含原文
CREATE TABLE IF NOT EXISTS style_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    app TEXT,
    injected TEXT NOT NULL,
    edited TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS style_traits (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    profile TEXT NOT NULL,
    samples INTEGER NOT NULL,
    updated REAL NOT NULL
);
"""

# 词条质量闸门参数
MAX_TERM_LEN = 24       # 单侧长度上限（再长就不是词条，是整句）
MIN_RATIO = 0.25        # 两侧长度比下限（防「删了一整段」被当成纠错）
MAX_RATIO = 4.0
HOTWORD_MAX_LEN = 12    # ASR 热词长度上限：整句级词条拼进 context 只会稀释热词

_MARKS = re.compile(r"[^\w]", re.UNICODE)   # 非文字字符（标点/空白）


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def _char_class(ch: str) -> str:
    if _is_cjk(ch):
        return "cjk"
    if ch.isdigit():
        return "digit"
    if ch.isascii() and ch.isalpha():
        return "latin"
    return "sym"


def is_learnable(wrong: str, right: str) -> bool:
    """词条质量闸门：不合格的改动宁可不学，也不能污染下游。"""
    w, r = wrong.strip(), right.strip()
    if not w or not r or w == r:
        return False
    if len(w) > MAX_TERM_LEN or len(r) > MAX_TERM_LEN:
        return False
    if not MIN_RATIO <= len(w) / len(r) <= MAX_RATIO:
        return False
    if _MARKS.sub("", w) == _MARKS.sub("", r):
        return False    # 只动了空白/标点，不是词条
    if len(w) == 1 and len(r) == 1 and _char_class(w) == _char_class(r):
        # 同类单字替换（我→你、a→e、5→7）几乎全是噪音，入了库就是全局误伤。
        # 度→℃（cjk→sym）、瓦→W（cjk→latin）这类跨类符号/单位修正照常放行。
        return False
    return True


class MemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self.path = path
        log.info("记忆库就绪: %s", path)

    # ---- 词典 ----

    def add_correction(self, wrong: str, right: str, trusted: bool = False) -> bool:
        """写入纠错对，返回是否入库。

        trusted=False（自动学习 / Ctrl+Fn）时先过 is_learnable 闸门；
        trusted=True 用于 `dict add` 这类用户显式指定的词条。
        """
        wrong, right = wrong.strip(), right.strip()
        if not wrong or not right or wrong == right:
            return False
        if not trusted and not is_learnable(wrong, right):
            log.info("词条被质量闸门拦下: %r → %r", wrong, right)
            return False
        now = time.time()
        self._conn.execute(
            """INSERT INTO dictionary (wrong, right, count, first_seen, last_seen)
               VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(wrong, right) DO UPDATE SET count = count + 1, last_seen = ?""",
            (wrong, right, now, now, now),
        )
        self._conn.commit()
        log.info("词典入库: %s → %s", wrong, right)
        return True

    def remove_correction(self, wrong: str, right: str) -> int:
        cur = self._conn.execute(
            "DELETE FROM dictionary WHERE wrong = ? AND right = ?", (wrong, right))
        self._conn.commit()
        return cur.rowcount

    def mappings(self, min_count: int = 1) -> list[tuple[str, str]]:
        """纠错映射（按频次降序），喂给润色 prompt。"""
        rows = self._conn.execute(
            "SELECT wrong, right FROM dictionary WHERE count >= ? ORDER BY count DESC",
            (min_count,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def hotwords(self, limit: int = 80) -> str:
        """高频正确形式，空格拼接，喂给 ASR context。

        只取「词条」不取整句：ASR 的 context 是有限的热词槽位，
        塞进 "the cost of the mortgages" 这种整句只会稀释真正的专名偏置。
        """
        rows = self._conn.execute(
            """SELECT right, SUM(count) AS c FROM dictionary
               WHERE length(right) BETWEEN 2 AND ?
                 AND instr(right, ' ') = 0
                 AND instr(right, char(10)) = 0
               GROUP BY right ORDER BY c DESC LIMIT ?""",
            (HOTWORD_MAX_LEN, limit),
        ).fetchall()
        return " ".join(r[0] for r in rows)

    def list_dictionary(self) -> list[tuple[str, str, int]]:
        return self._conn.execute(
            "SELECT wrong, right, count FROM dictionary ORDER BY count DESC").fetchall()

    # ---- 历史 ----

    def log_history(self, raw: str, polished: str, app: str = "") -> None:
        self._conn.execute(
            "INSERT INTO history (ts, app, raw, polished) VALUES (?, ?, ?, ?)",
            (time.time(), app, raw, polished),
        )
        self._conn.commit()

    # ---- 风格画像（P1b） ----

    def log_style_edit(self, injected: str, edited: str, app: str = "") -> None:
        """记录一次用户编辑（自动学习监视到的稳定改动）。词典对之外的
        风格类改动（拆句/换语气/重排格式）也进这张表，是画像原料。"""
        self._conn.execute(
            "INSERT INTO style_edits (ts, app, injected, edited) VALUES (?, ?, ?, ?)",
            (time.time(), app, injected, edited),
        )
        self._conn.commit()

    def recent_style_edits(self, limit: int = 60) -> list[tuple[str, str, str]]:
        """最近的编辑原样 [(app, injected, edited)]，新→旧。"""
        rows = self._conn.execute(
            "SELECT app, injected, edited FROM style_edits ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r[0] or "", r[1], r[2]) for r in rows]

    def style_edit_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM style_edits").fetchone()[0]

    def save_style_profile(self, profile: str, samples: int) -> None:
        """写入提炼后的风格画像（单行表，幂等覆盖）。"""
        self._conn.execute(
            "INSERT INTO style_traits (id, profile, samples, updated) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET profile=excluded.profile, "
            "samples=excluded.samples, updated=excluded.updated",
            (profile, samples, time.time()),
        )
        self._conn.commit()

    def style_profile(self) -> str:
        """当前风格画像；未提炼过返回空串。"""
        row = self._conn.execute(
            "SELECT profile FROM style_traits WHERE id = 1").fetchone()
        return row[0] if row else ""

    def style_profile_meta(self) -> tuple[int, float] | None:
        """(样本数, 提炼时间)；未提炼过返回 None。"""
        row = self._conn.execute(
            "SELECT samples, updated FROM style_traits WHERE id = 1").fetchone()
        return (row[0], row[1]) if row else None
