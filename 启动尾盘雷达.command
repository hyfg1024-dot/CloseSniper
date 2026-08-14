#!/bin/zsh
set -e

LAUNCHER_PATH="$0"
while [ -L "$LAUNCHER_PATH" ]; do
  LINK_DIR="$(cd "$(dirname "$LAUNCHER_PATH")" && pwd)"
  LINK_TARGET="$(readlink "$LAUNCHER_PATH")"
  if [[ "$LINK_TARGET" = /* ]]; then
    LAUNCHER_PATH="$LINK_TARGET"
  else
    LAUNCHER_PATH="$LINK_DIR/$LINK_TARGET"
  fi
done

SCRIPT_DIR="$(cd "$(dirname "$LAUNCHER_PATH")" && pwd)"
cd "$SCRIPT_DIR"

if curl -fsS http://127.0.0.1:8501/_stcore/health >/dev/null 2>&1; then
  echo "尾盘狙击 CloseSniper 已在运行，正在打开页面…"
  open http://127.0.0.1:8501
  exit 0
fi

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

echo "尾盘狙击 CloseSniper 即将打开。关闭此窗口即可停止程序。"
python -m streamlit run app.py --server.address 127.0.0.1 --server.headless false --browser.gatherUsageStats false
