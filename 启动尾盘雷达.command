#!/bin/zsh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display dialog "未找到 Python 3。请先从 python.org 安装 Python 3.11 或更高版本。" buttons {"好"} default button "好" with icon stop'
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "首次启动：正在创建独立环境…"
  python3 -m venv .venv
fi

source .venv/bin/activate
if ! python -c "import streamlit, akshare, pandas, numpy, plotly" >/dev/null 2>&1; then
  echo "首次启动：正在安装所需组件…"
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt
fi

echo "尾盘雷达即将打开。关闭此窗口即可停止程序。"
python -m streamlit run app.py --server.headless false --browser.gatherUsageStats false
