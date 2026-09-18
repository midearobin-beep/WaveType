"""记忆层：SQLite 个人词典 + 历史记录。

飞轮设计（对标 Typeless Personal Dictionary，但数据完全本地）：
- dictionary：纠错对（wrong → right），count 累计命中次数
- history：每次听写的 raw ASR / 润色结果 / 前台 App，供后续风格画像使用
- 词典回注两条路：ASR context 热词（源头防错）+ 润色 prompt 映射（兜底纠错）
"""
from __future__ import annotations

import logging
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
"""


class MemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        log.info("记忆库就绪: %s", path)

    # ---- 词典 ----

    def add_correction(self, wrong: str, right: str) -> None:
        wrong, right = wrong.strip(), right.strip()
        if not wrong or not right or wrong == right:
            return
        now = time.time()
        self._conn.execute(
            """INSERT INTO dictionary (wrong, right, count, first_seen, last_seen)
               VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(wrong, right) DO UPDATE SET count = count + 1, last_seen = ?""",
            (wrong, right, now, now, now),
        )
        self._conn.commit()
        log.info("词典入库: %s → %s", wrong, right)

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
        """高频正确形式，空格拼接，喂给 ASR context。"""
        rows = self._conn.execute(
            """SELECT right, SUM(count) AS c FROM dictionary
               GROUP BY right ORDER BY c DESC LIMIT ?""",
            (limit,),
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
