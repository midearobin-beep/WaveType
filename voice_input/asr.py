"""ASR：Qwen3-ASR 本地转写（MLX 后端，Apple Silicon 原生 Metal 加速）。

面向语音输入的短音频场景（秒级）：
- 不需要 VAD 分窗（push-to-talk 天然切分）
- Session 常驻内存，换取响应速度
- context 热词参数：个人词典经此注入，从源头减少术语误识别
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import numpy as np

# 模型使用本地路径，禁止 HF Hub 联网（否则走 Veee 代理会 502/超时）
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from .recorder import save_wav

log = logging.getLogger(__name__)


class ASR:
    def __init__(self, model: str = "mlx-community/Qwen3-ASR-1.7B-5bit",
                 language: str = "auto", context: str = "") -> None:
        from mlx_qwen3_asr import Session
        model = str(Path(model).expanduser())  # 展开 ~，本地路径避免被当成 HF repo id
        log.info("加载 ASR 模型 %s …", model)
        self._session = Session(model=model)
        self._language = None if language in ("auto", "", None) else language
        self._context = context
        log.info("ASR 就绪")

    def set_context(self, context: str) -> None:
        """更新热词（个人词典入口）。"""
        self._context = context

    def transcribe(self, audio: np.ndarray) -> str:
        if len(audio) < 1600:  # <0.1s，误触
            return ""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            save_wav(audio, tmp_path)
            kwargs = {"return_timestamps": False}
            if self._language:
                kwargs["language"] = self._language
            if self._context:
                kwargs["context"] = self._context
            result = self._session.transcribe(str(tmp_path), **kwargs)
            text = (result.text or "").strip()
            log.info("ASR: %s", text)
            return text
        finally:
            tmp_path.unlink(missing_ok=True)
