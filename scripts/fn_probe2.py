#!/usr/bin/env python3
"""Fn 键三路探测：判断 Fn 在哪一层还能拿到。

用法（必须在「终端」里跑，终端需有 输入监控 权限）：
    cd /Users/robinwang/Coding/语音输入本地部署
    .venv/bin/python scripts/fn_probe2.py

运行后 15 秒内：按住 Fn 约 2 秒再松开，重复 3 次；最后再按一次 Fn+Shift。

三路来源：
  [tap]  CG event tap 的 flagsChanged（现有 hotkey.py 用的就是这一路）
  [poll] CGEventSourceFlagsState 20ms 轮询（HID / Session 两种状态）
  [hid]  IOHIDManager 直接读 HID 值（WindowServer 之前；Apple Fn = page 0xFF/0xFF01, usage 0x03）
"""
from __future__ import annotations

import ctypes
import ctypes.util
import threading
import time

import Quartz

FN_FLAG = 0x800000
DURATION = float(__import__("os").environ.get("PROBE_SECS", "15"))

stats = {"tap": 0, "poll_hid": 0, "poll_session": 0, "hid_fn": 0}
hid_vendor_seen: dict[tuple[int, int], int] = {}
t0 = time.monotonic()


def ts() -> str:
    return f"{time.monotonic() - t0:6.2f}s"


# ---------- [tap] ----------

def tap_cb(proxy, etype, event, refcon):
    if etype == Quartz.kCGEventFlagsChanged:
        flags = Quartz.CGEventGetFlags(event)
        fn = bool(flags & FN_FLAG)
        if fn:
            stats["tap"] += 1
        print(f"{ts()} [tap]  flags={flags:#x} fn={fn}")
    return event


# ---------- [poll] ----------

def poll_loop(stop: threading.Event) -> None:
    last = {}
    states = {"poll_hid": Quartz.kCGEventSourceStateHIDSystemState,
              "poll_session": Quartz.kCGEventSourceStateCombinedSessionState}
    while not stop.is_set():
        for name, st in states.items():
            fn = bool(Quartz.CGEventSourceFlagsState(st) & FN_FLAG)
            if fn != last.get(name, False):
                print(f"{ts()} [{name}] fn={'按下' if fn else '松开'}")
                if fn:
                    stats[name] += 1
                last[name] = fn
        time.sleep(0.02)


# ---------- [hid] ----------

iokit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
vp = ctypes.c_void_p
iokit.IOHIDManagerCreate.restype = vp
iokit.IOHIDManagerCreate.argtypes = [vp, ctypes.c_uint32]
iokit.IOHIDManagerSetDeviceMatching.argtypes = [vp, vp]
iokit.IOHIDManagerOpen.restype = ctypes.c_int32
iokit.IOHIDManagerOpen.argtypes = [vp, ctypes.c_uint32]
iokit.IOHIDManagerScheduleWithRunLoop.argtypes = [vp, vp, vp]
iokit.IOHIDValueGetElement.restype = vp
iokit.IOHIDValueGetElement.argtypes = [vp]
iokit.IOHIDValueGetIntegerValue.restype = ctypes.c_long
iokit.IOHIDValueGetIntegerValue.argtypes = [vp]
iokit.IOHIDElementGetUsagePage.restype = ctypes.c_uint32
iokit.IOHIDElementGetUsagePage.argtypes = [vp]
iokit.IOHIDElementGetUsage.restype = ctypes.c_uint32
iokit.IOHIDElementGetUsage.argtypes = [vp]
cf.CFRunLoopGetCurrent.restype = vp
VALUE_CB = ctypes.CFUNCTYPE(None, vp, ctypes.c_int32, vp, vp)
iokit.IOHIDManagerRegisterInputValueCallback.argtypes = [vp, VALUE_CB, vp]

_VENDOR_PAGES = {0xFF, 0xFF01, 0xFF00}


def _hid_value(ctx, result, sender, value):
    el = iokit.IOHIDValueGetElement(value)
    page = iokit.IOHIDElementGetUsagePage(el)
    usage = iokit.IOHIDElementGetUsage(el)
    v = iokit.IOHIDValueGetIntegerValue(value)
    if page in _VENDOR_PAGES:
        hid_vendor_seen[(page, usage)] = hid_vendor_seen.get((page, usage), 0) + 1
        is_fn = usage == 0x03
        if is_fn and v:
            stats["hid_fn"] += 1
        print(f"{ts()} [hid]  page={page:#x} usage={usage:#x} value={v}"
              + ("   ← Fn" if is_fn else ""))
    elif page == 0x07 and 0xE0 <= usage <= 0xE7:  # 标准修饰键，做对照
        print(f"{ts()} [hid]  modifier usage={usage:#x} value={v}")


_hid_cb_ref = VALUE_CB(_hid_value)


def start_hid() -> None:
    mgr = iokit.IOHIDManagerCreate(None, 0)
    iokit.IOHIDManagerSetDeviceMatching(mgr, None)  # 全部设备，回调里过滤
    iokit.IOHIDManagerRegisterInputValueCallback(mgr, _hid_cb_ref, None)
    mode = vp.in_dll(cf, "kCFRunLoopDefaultMode")
    iokit.IOHIDManagerScheduleWithRunLoop(mgr, cf.CFRunLoopGetCurrent(), mode)
    rc = iokit.IOHIDManagerOpen(mgr, 0)
    print(f"IOHIDManagerOpen = {rc:#x}（0 为成功）")
    globals()["_hid_mgr"] = mgr  # 防回收


def main() -> None:
    print("=" * 60)
    print("请在 15 秒内：按住 Fn 2 秒再松开 ×3，最后按一次 Fn+Shift")
    print("=" * 60)
    print(f"输入监控授权：{Quartz.CGPreflightListenEventAccess()}")

    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged), tap_cb, None)
    if tap is None:
        print("CGEventTapCreate 失败（无输入监控权限）")
    else:
        src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src,
                                  Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)

    start_hid()
    stop = threading.Event()
    threading.Thread(target=poll_loop, args=(stop,), daemon=True).start()

    Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, DURATION, False)
    stop.set()

    print("\n" + "=" * 60)
    print("结论（数字 = 检测到 Fn 按下的次数）")
    for k, n in stats.items():
        print(f"  {k:13s} {n}")
    if hid_vendor_seen:
        print("  厂商页 HID 元素：", {f"{p:#x}/{u:#x}": c for (p, u), c in hid_vendor_seen.items()})
    if stats["hid_fn"]:
        print("→ HID 层能拿到 Fn：可用方案 A（IOHIDManager）恢复 Fn")
    elif stats["poll_hid"] or stats["poll_session"]:
        print("→ 轮询能拿到 Fn：可用方案 B（20ms 轮询）恢复 Fn")
    elif stats["tap"]:
        print("→ tap 能拿到 Fn：现有实现应可工作，问题在别处")
    else:
        print("→ 三路都拿不到 Fn：只能用备用键")


if __name__ == "__main__":
    main()
