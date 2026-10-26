#!/usr/bin/env python3
"""WaveType app 图标生成：深色圆角底 + Siri 配色正弦波（与 HUD 调色板一致）。

运行：/Users/robinwang/.workbuddy/binaries/python/envs/default/bin/python assets/make_icon.py
产出：assets/icon.png（1024×1024）+ assets/icon.iconset/（多尺寸，可 iconutil 转 .icns）
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent
SIZE = 1024
SS = 2  # 超采样抗锯齿

# 与 voice_input/hud.py 的 _PALETTE 一致（cyan → blue → violet → magenta → red → orange）
PALETTE = [
    (0.20, 0.88, 0.90),
    (0.23, 0.48, 1.00),
    (0.54, 0.36, 1.00),
    (0.88, 0.30, 0.85),
    (1.00, 0.35, 0.35),
    (1.00, 0.62, 0.26),
]


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def palette_color(t: float) -> tuple[int, int, int]:
    """t ∈ [0,1] 沿调色板环形插值。"""
    t = t % 1.0
    pos = t * len(PALETTE)
    i = int(pos) % len(PALETTE)
    j = (i + 1) % len(PALETTE)
    f = pos - int(pos)
    c = tuple(int(255 * _lerp(PALETTE[i][k], PALETTE[j][k], f)) for k in range(3))
    return c


def rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def make_background(s: int) -> Image.Image:
    """深色底：上 #161A2E → 下 #0A0C16 纵向渐变 + 中心微光。"""
    img = Image.new("RGB", (s, s))
    top = (22, 26, 46)
    bot = (10, 12, 22)
    d = ImageDraw.Draw(img)
    for y in range(s):
        t = y / (s - 1)
        d.line([(0, y), (s, y)], fill=tuple(int(_lerp(top[k], bot[k], t)) for k in range(3)))
    # 中心径向微光（很克制）
    glow = Image.new("L", (s, s), 0)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([s * 0.18, s * 0.22, s * 0.82, s * 0.78], fill=70)
    glow = glow.filter(ImageFilter.GaussianBlur(s * 0.16))
    tint = Image.new("RGB", (s, s), (40, 52, 110))
    img = Image.composite(tint, img, glow.point(lambda v: v // 3))
    return img


def envelope(x: float) -> float:
    """振幅包络：两端渐细（HUD 的 taper 风格），中间饱满。"""
    # x ∈ [0,1]；sin 形钟罩 + 端点压零
    return math.sin(math.pi * x) ** 1.4


def make_wave(s: int) -> Image.Image:
    """正弦波图层（透明底）：主波 + 次谐波，颜色沿 x 走 Siri 渐变。"""
    layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx = s / 2
    cy = s / 2
    span = s * 0.72          # 波形总宽
    amp = s * 0.175          # 最大振幅
    periods = 1.75           # 周期数
    n = 640
    base_w = s * 0.028       # 基准线宽

    def draw_wave(amp_scale: float, phase: float, width_scale: float,
                 alpha: int, color_shift: float) -> None:
        for i in range(n):
            t = i / (n - 1)
            x = cx - span / 2 + span * t
            env = envelope(t)
            y = cy - amp * amp_scale * env * math.sin(2 * math.pi * periods * t + phase)
            r = max(1.0, base_w * width_scale * (0.25 + 0.75 * env))
            col = palette_color(t + color_shift)
            d.ellipse([x - r, y - r, x + r, y + r], fill=col + (alpha,))

    # 次谐波：更细更淡、相位错开，增加层次
    draw_wave(0.45, phase=math.pi / 3, width_scale=0.45, alpha=110, color_shift=0.08)
    # 主波
    draw_wave(1.0, phase=0.0, width_scale=1.0, alpha=255, color_shift=0.0)
    return layer


def render(size: int = SIZE) -> Image.Image:
    s = size * SS
    bg = make_background(s).convert("RGBA")
    wave = make_wave(s)

    # 辉光：波形模糊放大铺底（两层，克制）
    glow = wave.filter(ImageFilter.GaussianBlur(s * 0.022))
    bg.alpha_composite(glow)
    bg.alpha_composite(glow.filter(ImageFilter.GaussianBlur(s * 0.012)))
    # 主波形
    bg.alpha_composite(wave)

    # macOS 圆角（约 22.37%）
    mask = rounded_mask(s, int(s * 0.2237))
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(bg, (0, 0), mask)
    return out.resize((size, size), Image.LANCZOS)


def main() -> None:
    icon = render(SIZE)
    icon.save(OUT / "icon.png")
    print(f"✓ {OUT / 'icon.png'}")

    iconset = OUT / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    for px in (16, 32, 64, 128, 256, 512):
        icon.resize((px, px), Image.LANCZOS).save(iconset / f"icon_{px}x{px}.png")
        icon.resize((px * 2, px * 2), Image.LANCZOS).save(iconset / f"icon_{px}x{px}@2x.png")
    print(f"✓ {iconset}/")

    # 有 iconutil 就顺手编一个 .icns
    try:
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT / "icon.icns")],
                       check=True, capture_output=True)
        print(f"✓ {OUT / 'icon.icns'}")
    except Exception as e:
        print(f"（跳过 .icns：{e}）")


if __name__ == "__main__":
    main()
