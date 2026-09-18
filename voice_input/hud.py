"""Waveform HUD：macOS 浮层波形（需求文档实现）。

架构（对应需求 §Rendering architecture）：
- WaveformHUDController：NSPanel（无边框/透明/不抢焦点/忽略鼠标/浮于普通窗口）
- WaveformModel：revealProgress / amplitudeReveal / phase / 渐变相位（纯状态，无 UI）
- 渲染：CAShapeLayer（主线）+ CAGradientLayer（颜色，以主线为 mask）
        + 辉光 = 同路径粗描边 + shadowRadius（克制的柔光，非霓虹）
- 电平：外部 provider 回调返回 RMS，VoiceLevelProcessor 归一化

视觉层级：voice → motion → color；无容器、无图标、无文字。
出现：ease-out，从中心点向左右展开 + 振幅从零升起。
消失：ease-in 稍快，两端向中心收拢至 ~15px 后淡出（非纯 opacity 动画）。
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable

import objc
import Quartz
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSColor,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSObject, NSTimer

from .level import VoiceLevelProcessor

log = logging.getLogger(__name__)


# ---------------- 可调常量（需求 §Goal） ----------------
class C:
    HUD_WIDTH = 380.0           # 320–420
    HUD_HEIGHT = 88.0           # 70–100
    BOTTOM_MARGIN = 128.0       # 距屏幕底边 100–160
    LINE_WIDTH = 1.9            # 1.5–2.2（中心最粗，两端经 taper mask 渐细）
    TAPER_RATIO = 0.18          # 两端渐细区占比（更明显的锥形）
    GLOW_STRENGTH = 0.22        # 辉光不透明度基线（15–30%）
    GRADIENT_SPEED = 0.05       # Listening 渐变流动（相位/秒）
    THINK_GRADIENT_SPEED = 0.02  # Thinking 更慢
    PHASE_SPEED = 1.6           # 波形相位推进（弧度/秒，慢而流动）
    MIN_AMPLITUDE = 0.0         # px，静默时完全平线
    MAX_AMPLITUDE = 34.0        # px，不触顶
    APPEAR_DURATION = 0.38      # s，ease-out
    DISAPPEAR_DURATION = 0.26   # s，ease-in 稍快
    FPS = 1.0 / 60.0
    SAMPLES = 140               # 每帧采样点数（连续平滑曲线）
    COLLAPSE_FADE_PX = 18.0     # 收拢到该半宽后开始淡出
    LABEL_FONT_SIZE = 11.0      # Listening / Thinking 标签（系统字体）
    # Thinking 波形：两组低幅波包从中心向两侧传播再折返
    THINK_AMPLITUDE = 7.0       # px，低幅
    THINK_PACKET_SIGMA = 26.0   # 波包宽度
    THINK_BASE_SPEED = 0.55     # 传播基频（弧度/秒）
    THINK_WOBBLE = 0.18         # 节奏微扰（规律但不可完全预测）
    THINK_CENTER_BOOST = 0.7    # 中心叠加时的峰值增益
    THINK_DESATURATE = 0.18     # 降饱和比例


# Siri 风调色板：cyan → blue → violet → magenta → warm red → orange（首尾相接成环）
_PALETTE = [
    (0.20, 0.88, 0.90),  # cyan
    (0.23, 0.48, 1.00),  # blue
    (0.54, 0.36, 1.00),  # violet
    (0.88, 0.30, 0.85),  # magenta
    (1.00, 0.35, 0.35),  # warm red
    (1.00, 0.62, 0.26),  # orange
]


def _palette_color(t: float, thinking: bool = False) -> tuple[float, float, float]:
    """t ∈ [0,1) 环形插值取色，过渡极软。
    Thinking：同一 gradient，饱和度略低、偏 violet/blue 区段。"""
    t = t % 1.0
    if thinking:
        t = 0.05 + t * 0.55  # 压缩到 cyan→violet/magenta 区段，蓝紫占比上升
    n = len(_PALETTE)
    pos = t * n
    i = int(pos) % n
    f = pos - int(pos)
    a, b = _PALETTE[i], _PALETTE[(i + 1) % n]
    # 平滑插值（smoothstep），避免条带感
    f = f * f * (3 - 2 * f)
    rgb = [a[k] + (b[k] - a[k]) * f for k in range(3)]
    if thinking:
        gray = sum(rgb) / 3.0
        rgb = [c + (gray - c) * C.THINK_DESATURATE for c in rgb]
    return tuple(rgb)


def _ease_out(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def _ease_in(t: float) -> float:
    return t * t


class WaveformModel:
    """纯动画状态：与渲染解耦。state: listening / thinking。"""

    def __init__(self) -> None:
        self.reveal = 0.0          # 0..1 水平展开进度
        self.amp_reveal = 0.0      # 0..1 振幅显现进度
        self.phase = 0.0
        self.gradient_phase = 0.0
        self.level = 0.0           # 平滑后的 0..1 电平
        self.fade = 1.0            # 收拢末段淡出
        self.visible = False
        self.state = "listening"
        self.think_t = 0.0         # thinking 动画时间轴
        self.center_flash = 0.0    # 中心叠加峰强度（0..1，供辉光增强）
        self._target = 0.0
        self._processor = VoiceLevelProcessor()

    def set_active(self, active: bool) -> None:
        if active and not self.visible:
            self.visible = True
            self.reveal = 0.0
            self.amp_reveal = 0.0
            self.fade = 1.0
            self._processor.reset()
        self._target = 1.0 if active else 0.0

    def set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        if state == "thinking":
            self.think_t = 0.0

    def tick(self, dt: float, rms: float, speech_prob: float = 1.0) -> None:
        self.phase += dt * C.PHASE_SPEED
        speed = C.THINK_GRADIENT_SPEED if self.state == "thinking" else C.GRADIENT_SPEED
        self.gradient_phase += dt * speed
        if self.visible and self.state == "listening":
            self.level = self._processor.process(rms, speech_prob)  # 非人声=平线
        elif self.state == "thinking":
            self.think_t += dt
            self.level *= 0.95  # 听→想过渡时振幅收小
        # reveal 展开/收拢
        if self._target > 0.5:
            self.reveal = min(1.0, self.reveal + dt / C.APPEAR_DURATION)
            self.amp_reveal = min(1.0, self.amp_reveal + dt / (C.APPEAR_DURATION * 0.7))
        else:
            self.reveal = max(0.0, self.reveal - dt / C.DISAPPEAR_DURATION)
            self.amp_reveal = max(0.0, self.amp_reveal - dt / (C.DISAPPEAR_DURATION * 0.6))

    def _listening_points(self, cx, cy, half, max_half) -> list[tuple[float, float]]:
        amp = (C.MIN_AMPLITUDE + (C.MAX_AMPLITUDE - C.MIN_AMPLITUDE) * self.level) \
            * self.amp_reveal
        # 谐波权重随电平微调（complexity 是 subtle response）
        w2 = 0.12 + 0.13 * self.level
        w3 = 0.04 + 0.06 * self.level
        f1 = 2 * math.pi * 2.1 / C.HUD_WIDTH
        f2 = 2 * math.pi * 3.3 / C.HUD_WIDTH
        f3 = 2 * math.pi * 4.6 / C.HUD_WIDTH
        n = max(8, int(C.SAMPLES * (half / max_half)))
        pts = []
        for i in range(n + 1):
            t = i / n
            x_local = (t * 2 - 1) * half
            env = math.exp(-((x_local / max(half, 1.0)) ** 2) * 2.2)  # 中间强两端弱
            y = math.sin((cx + x_local) * f1 + self.phase) \
                + w2 * math.sin((cx + x_local) * f2 + self.phase * 1.4) \
                + w3 * math.sin((cx + x_local) * f3 - self.phase * 0.7)
            pts.append((cx + x_local, cy + y * amp * env * 0.75))
        return pts

    def _thinking_points(self, cx, cy, half, max_half) -> list[tuple[float, float]]:
        """两组低幅波包从中心向左右传播再折返，偶尔中心叠加成峰。"""
        t = self.think_t
        # 传播距离：0 → 远 → 回 0，带慢速节奏扰动（规律但不可完全预测）
        wobble = 1.0 + C.THINK_WOBBLE * math.sin(t * 0.13)
        d = half * 0.85 * abs(math.sin(t * C.THINK_BASE_SPEED * wobble))
        # 中心叠加增益：d 越小，双波包重叠越多
        overlap = math.exp(-((d / 30.0) ** 2))
        self.center_flash = overlap
        amp = C.THINK_AMPLITUDE * (1.0 + C.THINK_CENTER_BOOST * overlap) * self.amp_reveal
        kf = 2 * math.pi * 3.0 / C.HUD_WIDTH
        n = max(24, int(C.SAMPLES * (half / max_half)))
        pts = []
        for i in range(n + 1):
            ti = i / n
            x_local = (ti * 2 - 1) * half
            x = cx + x_local
            y = 0.0
            for s in (-1.0, 1.0):
                dx = x_local - s * d
                packet = math.exp(-((dx / C.THINK_PACKET_SIGMA) ** 2))
                y += packet * math.sin(dx * kf - s * self.phase * 0.8)
            env = math.exp(-((x_local / max(half, 1.0)) ** 2) * 1.6)
            pts.append((x, cy + y * amp * env))
        return pts

    def render_state(self) -> tuple[list[tuple[float, float]], float] | None:
        """返回 (采样点序列, 总不透明度)；无需渲染返回 None。"""
        if not self.visible:
            return None
        max_half = C.HUD_WIDTH / 2.0 - 8.0
        if self._target > 0.5:
            half = max_half * _ease_out(self.reveal)
        else:
            half = max_half * _ease_in(self.reveal)
        if self._target < 0.5:
            # 收拢到 ~15px 后淡出，完全消失则隐藏
            if half <= 2.0:
                self.visible = False
                return None
            self.fade = min(1.0, half / C.COLLAPSE_FADE_PX)
        else:
            self.fade = 1.0

        cx, cy = C.HUD_WIDTH / 2.0, C.HUD_HEIGHT / 2.0
        if self.state == "thinking":
            pts = self._thinking_points(cx, cy, half, max_half)
        else:
            pts = self._listening_points(cx, cy, half, max_half)
        return pts, self.fade


class WaveformHUDController(NSObject):
    """NSPanel 浮层 + CALayer 渲染。所有方法须主线程调用（用 AppHelper.callAfter）。"""

    def initWithLevelProvider_(self, provider: Callable[[], tuple[float, float]]):
        self = objc.super(WaveformHUDController, self).init()
        if self is None:
            return None
        self._provider = provider
        self._model = WaveformModel()
        self._last_tick = 0.0
        self._timer = None
        self._learn_timer = None
        self._build_panel()
        return self

    # ---------- UI 构建 ----------

    def _taper_mask(self):
        """两端逐渐变细的 alpha 渐变 mask（覆盖在描边层上）。"""
        m = Quartz.CAGradientLayer.layer()
        m.setFrame_(Quartz.CGRectMake(0, 0, C.HUD_WIDTH, C.HUD_HEIGHT))
        m.setStartPoint_(Quartz.CGPointMake(0, 0.5))
        m.setEndPoint_(Quartz.CGPointMake(1, 0.5))
        clear = NSColor.colorWithWhite_alpha_(0.0, 0.0).CGColor()
        mid = NSColor.colorWithWhite_alpha_(1.0, 0.35).CGColor()
        solid = NSColor.colorWithWhite_alpha_(1.0, 1.0).CGColor()
        m.setColors_([clear, mid, solid, solid, mid, clear])
        r = C.TAPER_RATIO
        m.setLocations_([0.0, r * 0.4, r, 1.0 - r, 1.0 - r * 0.4, 1.0])
        return m

    def _build_panel(self) -> None:
        screen = NSScreen.mainScreen().visibleFrame()
        x = screen.origin.x + (screen.size.width - C.HUD_WIDTH) / 2.0
        y = screen.origin.y + C.BOTTOM_MARGIN
        frame = Quartz.CGRectMake(x, y, C.HUD_WIDTH, C.HUD_HEIGHT)
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False,
        )
        panel.setLevel_(NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setIgnoresMouseEvents_(True)
        panel.setHasShadow_(False)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
        )

        view = NSView.alloc().initWithFrame_(
            Quartz.CGRectMake(0, 0, C.HUD_WIDTH, C.HUD_HEIGHT))
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(NSColor.clearColor().CGColor())

        # 主线（作为渐变 mask）；taper mask 让线两端变细
        self._line = Quartz.CAShapeLayer.layer()
        self._line.setFillColor_(NSColor.clearColor().CGColor())
        self._line.setStrokeColor_(NSColor.whiteColor().CGColor())
        self._line.setLineWidth_(C.LINE_WIDTH)
        self._line.setLineCap_(Quartz.kCALineCapRound)
        self._line.setLineJoin_(Quartz.kCALineJoinRound)
        self._line.setMask_(self._taper_mask())

        self._gradient = Quartz.CAGradientLayer.layer()
        self._gradient.setFrame_(Quartz.CGRectMake(0, 0, C.HUD_WIDTH, C.HUD_HEIGHT))
        self._gradient.setStartPoint_(Quartz.CGPointMake(0, 0.5))
        self._gradient.setEndPoint_(Quartz.CGPointMake(1, 0.5))
        self._gradient.setMask_(self._line)

        # 辉光：同路径、更粗、低透明度 + 柔 shadow
        self._glow_line = Quartz.CAShapeLayer.layer()
        self._glow_line.setFillColor_(NSColor.clearColor().CGColor())
        self._glow_line.setStrokeColor_(NSColor.whiteColor().CGColor())
        self._glow_line.setLineWidth_(C.LINE_WIDTH * 2.2)
        self._glow_line.setLineCap_(Quartz.kCALineCapRound)
        self._glow_line.setShadowColor_(NSColor.whiteColor().CGColor())
        self._glow_line.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        self._glow_line.setShadowRadius_(5.0)
        self._glow_line.setMask_(self._taper_mask())

        self._glow_gradient = Quartz.CAGradientLayer.layer()
        self._glow_gradient.setFrame_(Quartz.CGRectMake(0, 0, C.HUD_WIDTH, C.HUD_HEIGHT))
        self._glow_gradient.setStartPoint_(Quartz.CGPointMake(0, 0.5))
        self._glow_gradient.setEndPoint_(Quartz.CGPointMake(1, 0.5))
        self._glow_gradient.setOpacity_(C.GLOW_STRENGTH)
        self._glow_gradient.setMask_(self._glow_line)

        view.layer().addSublayer_(self._glow_gradient)
        view.layer().addSublayer_(self._gradient)

        # Listening / Thinking / 词条记录 标签（系统字体，居中于波形上方）
        from AppKit import NSFont, NSTextField
        label = NSTextField.labelWithString_("Listening")
        label.setFont_(NSFont.systemFontOfSize_weight_(C.LABEL_FONT_SIZE, 0.23))  # medium
        label.setTextColor_(NSColor.colorWithWhite_alpha_(1.0, 0.72))
        label.setAlignment_(1)  # center
        label.setLineBreakMode_(4)  # truncate tail
        lw = C.HUD_WIDTH - 40.0
        label.setFrame_(Quartz.CGRectMake(20.0, C.HUD_HEIGHT - 20.0, lw, 15.0))
        view.addSubview_(label)
        self._label = label

        panel.setContentView_(view)
        self._panel = panel
        self._update_gradient()

    def _update_gradient(self) -> None:
        thinking = self._model.state == "thinking"
        n = 8
        colors = []
        for i in range(n + 1):
            r, g, b = _palette_color(i / n + self._model.gradient_phase, thinking)
            colors.append(NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0).CGColor())
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self._gradient.setColors_(colors)
        self._glow_gradient.setColors_(colors)
        Quartz.CATransaction.commit()

    # ---------- 对外控制（主线程） ----------

    def show(self) -> None:
        self._model.set_active(True)
        if self._timer is None:
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                C.FPS, self, "tick:", None, True)
        self._panel.setAlphaValue_(1.0)
        self._panel.orderFrontRegardless()

    def hide(self) -> None:
        self._model.set_active(False)  # 收拢动画在 tick 里推进，完成后再 orderOut

    def setListening(self) -> None:
        self._model.set_state("listening")
        self._label.setStringValue_("Listening")
        self.show()

    def setThinking(self) -> None:
        self._model.set_state("thinking")
        self._label.setStringValue_("Thinking")
        self.show()  # 幂等：确保面板可见且 timer 在跑

    # ---------- 词条学习反馈 ----------

    def showLearned_(self, entry: str) -> None:
        """词条已记录：波形以 Thinking 视觉重现，2.6s 后自动收拢。"""
        self._label.setStringValue_(f'"{entry}" saved to dictionary')
        self._model.set_state("thinking")
        self.show()
        if self._learn_timer is not None:
            self._learn_timer.invalidate()
        self._learn_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            2.6, self, "autoHideLearned:", None, False)

    def autoHideLearned_(self, timer) -> None:
        self._learn_timer = None
        # 仅当仍处于词条提示状态才收拢（用户可能已开始新的听写）
        if self._model.state == "thinking":
            self.hide()

    # ---------- 帧循环 ----------

    def tick_(self, timer) -> None:
        import time
        now = time.monotonic()
        dt = min(0.05, now - self._last_tick) if self._last_tick else C.FPS
        self._last_tick = now
        rms, speech_prob = self._provider()
        self._model.tick(dt, rms, speech_prob)
        state = self._model.render_state()
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        if state is None:
            self._line.setPath_(None)
            self._glow_line.setPath_(None)
            Quartz.CATransaction.commit()
            if not self._model.visible:
                self._panel.orderOut_(None)
                if self._timer is not None and self._model._target < 0.5:
                    self._timer.invalidate()
                    self._timer = None
            return
        pts, fade = state
        path = Quartz.CGPathCreateMutable()
        Quartz.CGPathMoveToPoint(path, None, pts[0][0], pts[0][1])
        for x, y in pts[1:]:
            Quartz.CGPathAddLineToPoint(path, None, x, y)
        self._line.setPath_(path)
        self._glow_line.setPath_(path)
        # 辉光随音量轻微增强；thinking 中心叠加时偶尔更亮（magenta/cyan 聚合感）
        glow = C.GLOW_STRENGTH * (0.6 + 0.8 * self._model.level)
        if self._model.state == "thinking":
            glow = C.GLOW_STRENGTH * (0.5 + 0.9 * self._model.center_flash)
        self._glow_gradient.setOpacity_(glow * fade)
        self._glow_line.setShadowRadius_(4.0 + 5.0 * max(self._model.level,
                                                         self._model.center_flash * 0.8))
        self._gradient.setOpacity_(fade)
        self._update_gradient()
        Quartz.CATransaction.commit()


    # ---------- demo 定时退出 ----------

    def scheduleDemoQuit_(self, seconds: float) -> None:
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            seconds, self, "demoHide:", None, False)

    def demoHide_(self, timer) -> None:
        self.hide()
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, "demoTerminate:", None, False)

    def demoTerminate_(self, timer) -> None:
        from AppKit import NSApp
        NSApp.terminate_(None)

    def demoFlip_(self, timer) -> None:
        """demo 用：Listening → Thinking → 词条记录反馈 三态循环，便于调动画。"""
        if self._model.state == "listening":
            self.setThinking()
        else:
            self.showLearned_("teh → the")
            # showLearned_ 自带 2.6s 自动收拢；收拢后回到 listening 继续循环
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                3.2, self, "demoResumeListening:", None, False)

    def demoResumeListening_(self, timer) -> None:
        if self._model.state == "thinking":
            self.setListening()


def run_demo(seconds: float = 0.0) -> None:
    """Demo 模式：模拟电平，无需说话即可调动画。seconds>0 时自动退出。"""
    import math
    import time
    from PyObjCTools import AppHelper

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    t0 = time.monotonic()

    def fake_level() -> tuple[float, float]:
        t = time.monotonic() - t0
        # 模拟说话：慢包络 × 快抖动
        env = max(0.0, math.sin(t * 0.9)) ** 1.5
        return 0.005 + 0.09 * env * (0.6 + 0.4 * math.sin(t * 13.0)), 1.0

    hud = WaveformHUDController.alloc().initWithLevelProvider_(fake_level)
    hud.setListening()
    # 每 5 秒切换 Listening / Thinking，便于两种状态一起调
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        5.0, hud, "demoFlip:", None, True)
    if seconds > 0:
        hud.scheduleDemoQuit_(seconds)
    print(f"🌊 HUD demo 运行中（{'%.0fs 后自动退出' % seconds if seconds else 'Ctrl+C 退出'}）", flush=True)
    AppHelper.runEventLoop()
