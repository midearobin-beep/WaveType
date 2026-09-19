#!/usr/bin/env python3
"""入口：
  python main.py                启动语音输入
  python main.py --raw          跳过 LLM 润色
  python main.py dict           查看词典
  python main.py dict add 错误词 正确词   手动加词条
  python main.py dict del 错误词 正确词   删词条
"""
import argparse
import logging
import sys
from pathlib import Path

from voice_input.app import VoiceInputApp


def _dict_cmd(args) -> None:
    from voice_input.memory import MemoryStore
    store = MemoryStore(Path(__file__).parent / "data" / "memory.db")
    if args.action == "add":
        store.add_correction(args.wrong, args.right)
        print(f"已添加：{args.wrong} → {args.right}")
    elif args.action == "del":
        n = store.remove_correction(args.wrong, args.right)
        print("已删除" if n else "未找到该词条")
    else:
        rows = store.list_dictionary()
        if not rows:
            print("词典为空。用 Ctrl+Fn 纠错或 dict add 添加。")
        for wrong, right, count in rows:
            print(f"  {wrong} → {right}  (×{count})")


def main() -> None:
    ap = argparse.ArgumentParser(description="本地语音输入")
    ap.add_argument("-c", "--config", default=str(Path(__file__).parent / "config.yaml"))
    ap.add_argument("--raw", action="store_true", help="跳过 LLM 润色，直接上屏 ASR 原文")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--demo", action="store_true", help="HUD 演示模式：模拟电平，不用说话")
    ap.add_argument("--demo-secs", type=float, default=0, help="demo 自动退出秒数")
    ap.add_argument("--setup", action="store_true", help="打开首次运行设置向导（GUI）")
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("dict", help="词典管理")
    d.add_argument("action", nargs="?", default="list", choices=["list", "add", "del"])
    d.add_argument("wrong", nargs="?")
    d.add_argument("right", nargs="?")
    args = ap.parse_args()

    if args.cmd == "dict":
        if args.action in ("add", "del") and not (args.wrong and args.right):
            ap.error("dict add/del 需要：错误词 正确词")
        _dict_cmd(args)
        return

    if args.demo:
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        from voice_input.hud import run_demo
        run_demo(seconds=args.demo_secs)
        return

    if args.setup:
        import setup_wizard
        setup_wizard.run()
        return

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = VoiceInputApp(Path(args.config))
    if args.raw:
        app._polisher.enabled = False
    app.run()


if __name__ == "__main__":
    main()
