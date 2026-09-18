"""录音：sounddevice 16kHz mono，start/stop 语义（hold-to-talk 不需要 VAD）。"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class Recorder:
    def __init__(self, vad_model: str | None = None) -> None:
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._recording = False
        self.level = 0.0        # 实时 RMS（回调线程写，HUD 线程读；float 读写原子够用）
        self.speech_prob = 1.0  # 语音概率（无 VAD 时恒 1.0，退化为能量门）
        self._vad = None
        if vad_model:
            from .vad import create_vad
            self._vad = create_vad(vad_model)

    @property
    def recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        with self._lock:
            if self._recording:
                return
            self._frames = []
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                callback=self._cb, blocksize=1024,
            )
            self._stream.start()
            self._recording = True
        log.info("录音开始")

    def stop(self) -> np.ndarray:
        with self._lock:
            self._recording = False
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self._stream = None
            frames = self._frames
            self._frames = []
        audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
        log.info("录音结束: %.1fs", len(audio) / SAMPLE_RATE)
        return audio.reshape(-1)

    def _cb(self, indata, frames, time_info, status) -> None:
        if status:
            log.warning("录音回调状态: %s", status)
        block = indata.copy()
        self._frames.append(block)
        self.level = float(np.sqrt(np.mean(block ** 2)))
        if self._vad is not None:
            self._vad.feed(block.reshape(-1))
            self.speech_prob = self._vad.prob


def save_wav(audio: np.ndarray, path: Path) -> Path:
    sf.write(str(path), audio, SAMPLE_RATE, subtype="PCM_16")
    return path
