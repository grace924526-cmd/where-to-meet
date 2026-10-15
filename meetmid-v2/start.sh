#!/bin/bash
set -e

echo "🚀 启动下班顺路吃饭推荐..."

# 检查 Python 版本
python3 --version | grep -E "3\.(10|11|12|13)" > /dev/null 2>&1 || {
    echo "❌ 需要 Python 3.10+"
    exit 1
}

# 创建虚拟环境（如果不存在）
if [ ! -d "venv" ]; then
    echo "📦 创建虚拟环境..."
    python3 -m venv venv
fi

# 激活虚拟环境
source venv/bin/activate

# 安装依赖
echo "📦 安装依赖..."
pip install -r requirements.txt -q

# 检查 .env 文件
if [ ! -f ".env" ]; then
    echo "❌ 缺少 .env 文件，请复制 .env.example 并填入 API Key"
    exit 1
fi

echo "✅ 环境就绪，启动服务..."
python app_v2.py
