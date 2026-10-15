#!/bin/bash
set -e

echo "💥 KABOOM! 启动下班顺路吃饭推荐..."

# 检查 .env 文件
if [ ! -f ".env" ]; then
    echo "⚠️  未找到 .env 文件，请先复制 .env.example 并填写 API Key："
    echo "    cp .env.example .env"
    exit 1
fi

# 检查 uv 是否安装
if command -v uv &> /dev/null; then
    echo "✅ 使用 uv 管理依赖..."
    uv venv --python 3.10 2>/dev/null || uv venv
    source .venv/bin/activate
    uv pip install -r requirements.txt
else
    echo "✅ 使用 pip 管理依赖..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
fi

echo "🚀 启动服务，访问 http://localhost:5000"
python app_v2.py
