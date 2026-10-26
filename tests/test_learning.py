#!/usr/bin/env python3
"""词库学习链路的离线单测（不依赖 pytest，直接跑）。

    ./.venv/bin/python3 tests/test_learning.py

覆盖：质量闸门 is_learnable、自动学习 extract_pairs、Ctrl+Fn extract_wrong、
词典入库/hotwords 过滤，以及 AutoLearner 的端到端闭环（用假的焦点输入框）。
"""
from __future__ import annotations

import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice_input import autolearn  # noqa: E402
from voice_input.autolearn import AutoLearner, extract_pairs  # noqa: E402
from voice_input.learn import extract_wrong  # noqa: E402
from voice_input.memory import MemoryStore, is_learnable  # noqa: E402

FAILED: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILED.append(name)
    print(f"  {'✓' if ok else '✗'} {name:52} got={got!r}" + ("" if ok else f"  want={want!r}"))


# ---------- 1. 质量闸门（词典会以「必须严格遵守」进 prompt，垃圾词条会全局误伤）----------

def test_gate() -> None:
    print("\n1. is_learnable 质量闸门")
    check("同音专名", is_learnable("万巨龙", "万居隆"), True)
    check("跨类单字（度→℃）", is_learnable("度", "℃"), True)
    check("跨类单字（瓦→W）", is_learnable("瓦", "W"), True)
    check("1→2 字（兆→MB）", is_learnable("兆", "MB"), True)
    check("同类单字（我→你）拒绝", is_learnable("我", "你"), False)
    check("同类单字（5→7）拒绝", is_learnable("5", "7"), False)
    check("只改标点拒绝", is_learnable("你好。", "你好！"), False)
    check("只改空白拒绝", is_learnable("data sheet", "datasheet"), False)
    check("空串拒绝", is_learnable("", "万居隆"), False)
    check("相同拒绝", is_learnable("万居隆", "万居隆"), False)
    check("超长拒绝", is_learnable("一" * 30, "二"), False)
    check("长度比失衡拒绝", is_learnable("万居隆", "万居隆技术有限公司国际业务部"), False)


# ---------- 2. 自动学习：从（注入文本 → 用户改后文本）提纠错对 ----------

def test_extract_pairs() -> None:
    print("\n2. extract_pairs（自动学习）")
    cases = [
        ("请把万巨龙的报价发给我", "请把万居隆的报价发给我", [("万巨龙", "万居隆")]),
        ("这个 IPM 模块的死区是 5 度", "这个 IPM 模块的死区是 5℃", [("度", "℃")]),
        ("三花发的一个电控三维，有 70 多兆", "三花发的一个电控三维，有 70 多 MB", [("兆", "MB")]),
        ("压缩机的曲轴箱加热是 40 瓦", "压缩机的曲轴箱加热是 40W", [("瓦", "W")]),
        ("这个模块的充注量是六百克", "这个 IPM 模块的充注量是 600 克", [("六百", "600")]),
        ("家豪是什么意思", "家豪是什么意思？", []),
        ("我的方案给他看", "你的方案给他看", []),
        ("让我看看这个方案", "让我看看这个方案的细节，另外明天上午十点我们开个会", []),
        ("The the the cost of mortgages", "The cost of the mortgages", []),
        ("完全不同的两句话", "毫不相干的内容在这里", []),
    ]
    for injected, current, want in cases:
        check(f"{injected[:16]}… → {current[:16]}…", extract_pairs(injected, current), want)


# ---------- 3. Ctrl+Fn 手动纠错：从（上屏文本, 选区）定位错误形式 ----------

def test_extract_wrong() -> None:
    print("\n3. extract_wrong（Ctrl+Fn）")
    check("专名", extract_wrong("请把万巨龙的报价发给我", "万居隆"), "万巨龙")
    # 旧实现会返回 '5 '（选区的截断前缀）→ 存成 ("5","5℃")，把全文所有 5 都改掉
    check("符号（旧实现给 '5 '）", extract_wrong("这个 IPM 模块的死区是 5 度", "5℃"), "5 度")
    check("短语", extract_wrong("请把万巨龙的报价发给我", "万居隆的报价"), "万巨龙的报价")
    check("已是原文→无可学", extract_wrong("请把万巨龙的报价发给我", "万巨龙"), None)
    check("空选区", extract_wrong("请把万巨龙的报价发给我", ""), None)


# ---------- 4. 词典存储与 hotwords 过滤 ----------

def test_store() -> None:
    print("\n4. MemoryStore 入库与 hotwords 过滤")
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp) / "m.db")
        check("自动路径入库成功", store.add_correction("万巨龙", "万居隆"), True)
        check("自动路径被闸门拦下", store.add_correction("我", "你"), False)
        check("自动路径拦下标点变化", store.add_correction("你好。", "你好！"), False)
        check("命令行显式添加放行", store.add_correction("我", "你", trusted=True), True)
        store.add_correction("the the cost of the mortgages", "the cost of the mortgages",
                             trusted=True)
        check("重复入库只累加 count", (store.add_correction("万巨龙", "万居隆"), 
                                  store.list_dictionary()[0][2])[1] >= 2, True)
        hw = store.hotwords()
        check("热词含专名", "万居隆" in hw, True)
        check("热词剔除整句级词条", "mortgages" in hw, False)
        check("词典条数", len(store.list_dictionary()), 3)
        check("删除词条", store.remove_correction("万巨龙", "万居隆"), 1)


# ---------- 5. AutoLearner 端到端：注入 → 用户改 → 自动入库 + 回注 ----------

def test_autolearn_loop() -> None:
    print("\n5. AutoLearner 端到端闭环")

    class FakeFocused:
        """假焦点输入框：按脚本依次返回内容，模拟"注入后用户改了"。"""
        ok = True
        route = "fake"

        def __init__(self, script):
            self._script = list(script)
            self._last = self._script[-1]

        def value(self):
            return self._script.pop(0) if self._script else self._last

    class FakeTarget:
        def __init__(self, script):
            self._script = script
        def __call__(self, pid=None):
            return FakeFocused(self._script)

    injected = "请把万巨龙的报价发给我"
    edited = "请把万居隆的报价发给我"
    script = [injected, injected, edited, edited, edited]

    orig = (autolearn.FocusedText, autolearn.frontmost_app_name,
            autolearn.POLL_INTERVAL, autolearn.WATCH_SECONDS)
    autolearn.FocusedText = FakeTarget(script)
    autolearn.frontmost_app_name = lambda: "FakeApp"
    autolearn.POLL_INTERVAL = 0.01
    autolearn.WATCH_SECONDS = 3.0
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "m.db")
            learned: list[tuple[str, str]] = []
            learner = AutoLearner(store, lambda w, r: learned.append((w, r)))
            learner.watch(injected, app="FakeApp", pid=1)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not learned:
                time.sleep(0.02)
            learner.stop()
            check("自动学到词条", learned, [("万巨龙", "万居隆")])
            check("已落库", store.list_dictionary(), [("万巨龙", "万居隆", 1)])
            check("已进 ASR 热词", "万居隆" in store.hotwords(), True)

        # 定位不到输入框时必须留日志（旧实现静默 break，排障无痕）
        autolearn.FocusedText = FakeTarget([])
        class DeadTarget:
            ok = False
            route = "测试：无目标"
            def __init__(self, pid=None): pass
        autolearn.FocusedText = DeadTarget
        records: list[str] = []

        class Catcher(logging.Handler):
            def emit(self, rec):
                records.append(rec.getMessage())

        logger = logging.getLogger("voice_input.autolearn")
        handler = Catcher()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        with tempfile.TemporaryDirectory() as tmp:
            learner = AutoLearner(MemoryStore(Path(tmp) / "m.db"), lambda *a: None)
            learner.watch(injected, app="FakeApp", pid=1)
            time.sleep(0.2)
        logger.removeHandler(handler)
        check("定位失败有明确日志", any("未定位到目标输入框" in r for r in records), True)
    finally:
        (autolearn.FocusedText, autolearn.frontmost_app_name,
         autolearn.POLL_INTERVAL, autolearn.WATCH_SECONDS) = orig


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)  # 单测只看断言结果，压掉 INFO 噪音
    test_gate()
    test_extract_pairs()
    test_extract_wrong()
    test_store()
    test_autolearn_loop()
    print("\n" + "=" * 62)
    if FAILED:
        print(f"✗ {len(FAILED)} 项失败：")
        for f in FAILED:
            print("   -", f)
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
