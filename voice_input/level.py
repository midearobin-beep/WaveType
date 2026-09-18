"""麦克风电平处理：RMS → dBFS → 噪声门 → 归一化 → 压缩 → attack/release 平滑。

渲染层只消费归一化的 0..1 voiceLevel，不接触原始 PCM（需求 §Voice-driven）。
dBFS 映射：-60=静音，-30=小声，-20=正常，-10=强声，-3=最大视觉振幅。
"""
from __future__ import annotations

import math

# 可调常量
DB_FLOOR = -60.0      # 噪声门：以下视为静音
DB_CEIL = -3.0        # 最大视觉振幅
ATTACK_MS = 55.0      # 上升快
RELEASE_MS = 240.0    # 衰减慢
COMPRESSION = 0.7     # 非线性压缩指数（<1 = 抬升小声、压缩大声）
MIN_ACTIVE_LEVEL = 0.0  # 静默时振幅为零（平线），只有检测到语音才跳动


class VoiceLevelProcessor:
    def __init__(self) -> None:
        self._level = 0.0
        self._last_t: float | None = None

    def process(self, rms: float, now: float | None = None) -> float:
        import time
        now = time.monotonic() if now is None else now
        # RMS → dBFS → 归一化
        db = 20.0 * math.log10(max(rms, 1e-9))
        if db <= DB_FLOOR:
            target = 0.0
        else:
            target = min(1.0, (db - DB_FLOOR) / (DB_CEIL - DB_FLOOR))
            target = target ** COMPRESSION  # 柔和压缩
        # attack/release 平滑
        if self._last_t is None:
            dt = 0.016
        else:
            dt = min(0.1, max(0.001, now - self._last_t))
        self._last_t = now
        tau = ATTACK_MS if target > self._level else RELEASE_MS
        alpha = 1.0 - math.exp(-dt / (tau / 1000.0))
        self._level += (target - self._level) * alpha
        return self._level

    def reset(self) -> None:
        self._level = 0.0
        self._last_t = None
