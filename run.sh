#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "尚未安装环境，请先运行: bash setup.sh"
    exit 1
fi

CLASSNOTES_DEVICE="${CLASSNOTES_DEVICE:-auto}" .venv/bin/python cli.py
