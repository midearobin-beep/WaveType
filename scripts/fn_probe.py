#!/usr/bin/env python3
"""Fn 键诊断：判断 Fn 按下是否还进入 macOS 事件系统。

用法（在终端里跑，别在 WorkBuddy 里跑）：
    cd /Users/robinwang/Coding/语音输入本地部署
    .venv/bin/python scripts/fn_probe.py

运行后按住 Fn 约 2 秒再松开，重复 2~3 次，15 秒后自动出结论。

它会每 20ms 采样一次系统级修饰键状态（CGEventSourceFlagsState）。
这一步不依赖 event tap，所以能看到系统"认为"的 Fn 状态。
- 采样到 0x800000 → 系统知道 Fn 按下，问题出在 tap 层
- 采样不到        → Fn 在更底层就被吃掉，程序侧无解，只能换键
"""
from __future__ import annotations

import time

import Quartz

FN_FLAG = 0x800000
DURATION = 15.0


def main() -> None:
    print("=" * 60)
    print("Fn 键诊断")
    print("请在这 15 秒内【按住 Fn 约 2 秒再松开】，重复 2~3 次")
    print("=" * 60)

    try:
        ok = Quartz.CGPreflightListenEventAccess()
        print(f"输入监控授权：{'有' if ok else '无（tap 会收不到事件）'}")
    except Exception as exc:  # 老版本 pyobjc 没这函数
        print(f"输入监控授权：无法检测（{exc}）")

    state_map = {
        Quartz.kCGEventSourceStateHIDSystemState: "HID",
        Quartz.kCGEventSourceStateCombinedSessionState: "Session",
    }
    prev = {}
    seen_fn = False
    start = time.time()

    while time.time() - start < DURATION:
        for state, name in state_map.items():
            try:
                flags = Quartz.CGEventSourceFlagsState(state)
            except Exception:
                continue
            if prev.get(state) == flags:
                continue
            prev[state] = flags
            mark = "   <-- Fn 按下" if flags & FN_FLAG else ""
            if flags & FN_FLAG:
                seen_fn = True
            print(f"{time.strftime('%H:%M:%S')}  [{name}] flags={flags:#010x}{mark}")
        time.sleep(0.02)

    print("-" * 60)
    if seen_fn:
        print("结论：系统层能收到 Fn（flags 出现 0x800000）")
        print("      → 问题在 event tap 层：改 tap 挂载位置/选项有机会修好，不用换热键。")
    else:
        print("结论：15 秒内系统层从未出现 Fn 标志位")
        print("      → Fn 在进入 CG 事件系统之前就被消费了（或按键本身没产生事件）。")
        print("        程序侧改不动，只能换热键，或者回退系统版本。")


if __name__ == "__main__":
    main()
