#!/bin/bash
# WaveType 一键启动（在 Terminal.app 里运行本脚本，保持窗口开着）
cd "$(dirname "$0")" || exit 1

if [ ! -d .venv ]; then
    echo "未找到 .venv，正在创建并安装依赖…"
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pyobjc-framework-ApplicationServices
fi

if [ ! -f .env ]; then
    echo "⚠️  未找到 .env（云端润色需要 DEEPSEEK_API_KEY）"
    echo "    echo 'DEEPSEEK_API_KEY=sk-xxx' > .env"
    echo "    或把 config.yaml 的 llm.provider 改回 ollama 走本地模型"
fi

echo "🎙️  启动 WaveType（Ctrl+C 退出）"
exec .venv/bin/python main.py -v "$@"
