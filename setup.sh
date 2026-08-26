#!/bin/bash
# OpenClassNotes 一次性环境安装 (Linux / macOS)
set -e
cd "$(dirname "$0")"

mkdir -p mp3 transcripts notes graphs knowledge logs

echo "▸ 检查 Python..."
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "未找到 python3。请先安装 Python 3.10-3.12（推荐 3.11）。"
    exit 1
fi

"$PYTHON_BIN" -c 'import sys; assert (3, 10) <= sys.version_info[:2] < (3, 13), "需要 Python 3.10-3.12"' || {
    echo "Python 版本不兼容，请安装 Python 3.10-3.12（推荐 3.11）。"
    exit 1
}

echo "▸ 创建虚拟环境并安装依赖..."
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo ""
echo "=============================================="
echo " ✅ 安装完成! 现在运行:"
echo "    bash run.sh"
echo "    首次会引导你配置 LLM API key。"
echo "=============================================="
