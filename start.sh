#!/bin/bash
# WaveType 一键启动（在 Terminal.app 里运行本脚本，保持窗口开着）
cd "$(dirname "$0")" || exit 1

if [ ! -d .venv ]; then
    echo "未找到 .venv，正在创建并安装依赖…"
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pyobjc-framework-ApplicationServices
fi

# 首次运行：没有 config.yaml 时先打开图形化设置向导
if [ ! -f config.yaml ]; then
    echo "首次运行，打开设置向导…"
    .venv/bin/python setup_wizard.py
    [ -f config.yaml ] || { echo "向导未完成，已退出"; exit 1; }
fi

if [ ! -f .env ] && grep -q "provider: openai" config.yaml 2>/dev/null; then
    echo "⚠️  云端模式需要 .env（DEEPSEEK_API_KEY）"
    echo "    echo 'DEEPSEEK_API_KEY=sk-xxx' > .env"
    echo "    或把 config.yaml 的 llm.provider 改回 ollama 走本地模型"
fi

echo "🎙️  启动 WaveType（Ctrl+C 退出）"
exec .venv/bin/python main.py -v "$@"
