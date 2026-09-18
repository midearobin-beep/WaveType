"""Silero VAD v4（onnxruntime 推理）：区分"人声"与"其他声音"。

只服务于 HUD 波形显示门控——录音数据本身不受影响，
键盘声、关门声、风扇声不再引起波形跳动，只有检测到语音才动。

模型版本说明：使用 v4.0 tag 的 ONNX（h/c 双 LSTM 状态）。
master 分支的 v5 模型改了量程约定，直接喂 float32 归一化音频会近乎无响应，勿升级。

无依赖回退：模型缺失/onnxruntime 未安装时 speech_prob 恒为 1.0
（退化为纯能量门，不影响主流程）。
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_WINDOW = 512  # 16kHz 下 Silero 的标准窗口（32ms）


class SileroVAD:
    def __init__(self, model_path: str | Path) -> None:
        import onnxruntime as ort
        self._sess = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"])
        self._sr = np.array(16000, dtype=np.int64)
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._buf = np.zeros(0, dtype=np.float32)
        self.prob = 0.0  # 最近窗口的语音概率（0..1）

    def feed(self, pcm: np.ndarray) -> None:
        """喂入任意长度的 float32 PCM（16kHz mono），内部按 512 窗口推理。"""
        self._buf = np.concatenate([self._buf, pcm])
        while len(self._buf) >= _WINDOW:
            window, self._buf = self._buf[:_WINDOW], self._buf[_WINDOW:]
            out, self._h, self._c = self._sess.run(
                None,
                {"input": window[None, :], "sr": self._sr,
                 "h": self._h, "c": self._c},
            )
            self.prob = float(out[0][0])

    def reset(self) -> None:
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._buf = np.zeros(0, dtype=np.float32)
        self.prob = 0.0


def create_vad(model_path: str | Path) -> SileroVAD | None:
    """工厂：加载失败返回 None（调用方回退纯能量门）。"""
    path = Path(model_path).expanduser()
    if not path.exists():
        log.warning("VAD 模型不存在，回退能量门: %s", path)
        return None
    try:
        vad = SileroVAD(path)
        log.info("Silero VAD 就绪: %s", path.name)
        return vad
    except Exception:
        log.exception("VAD 加载失败，回退能量门")
        return None
