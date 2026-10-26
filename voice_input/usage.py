"""LLM token 用量记录：每次润色/翻译/问答调用落一行，供设置界面统计展示。

存进与词典同一个 SQLite（data/memory.db），独立 usage 表，互不干扰。
云端取响应的 usage 字段；本地 Ollama 取 prompt_eval_count / eval_count。
写入失败静默降级（记日志），绝不影响上屏主链路。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    feature TEXT NOT NULL,          -- polish | translate | ask
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
"""

_FEATURE_LABELS = {"polish": "润色", "translate": "翻译", "ask": "问答"}


class UsageLogger:
    def __init__(self, db_path: Path) -> None:
        self._db = Path(db_path)
        try:
            with self._conn() as c:
                c.executescript(_SCHEMA)
        except Exception:
            log.exception("usage 表初始化失败")

    def _conn(self) -> sqlite3.Connection:
        self._db.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self._db, timeout=5)

    def log(self, feature: str, model: str,
            prompt_tokens: int, completion_tokens: int) -> None:
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO usage(ts, feature, model, prompt_tokens, completion_tokens)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (time.time(), feature, model, prompt_tokens, completion_tokens))
        except Exception:
            log.exception("token 用量写入失败")

    def summary(self) -> dict:
        """返回 {'today': (p, c), 'total': (p, c), 'by_feature': [(label, p, c), ...]}"""
        empty = {"today": (0, 0), "total": (0, 0), "by_feature": []}
        try:
            with self._conn() as c:
                day_start = time.mktime(time.localtime()[:3] + (0, 0, 0) + (0, 0, -1))
                today = c.execute(
                    "SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0)"
                    " FROM usage WHERE ts >= ?", (day_start,)).fetchone()
                total = c.execute(
                    "SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0)"
                    " FROM usage").fetchone()
                rows = c.execute(
                    "SELECT feature, SUM(prompt_tokens), SUM(completion_tokens)"
                    " FROM usage GROUP BY feature ORDER BY 3 DESC").fetchall()
            return {
                "today": (int(today[0]), int(today[1])),
                "total": (int(total[0]), int(total[1])),
                "by_feature": [(_FEATURE_LABELS.get(f, f), int(p), int(cmp_))
                               for f, p, cmp_ in rows],
            }
        except Exception:
            log.exception("token 用量读取失败")
            return empty
