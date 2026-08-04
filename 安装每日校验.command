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

PROJECT_DIR="$(cd "$(dirname "$LAUNCHER_PATH")" && pwd)"
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/install_launch_agent.py" "$PROJECT_DIR"
osascript -e 'display dialog "已安装：每个工作日 09:45 和 10:30 分阶段校验前一交易日候选。若当时休眠，打开尾盘雷达后会自动补算。" buttons {"好"} default button "好" with icon note'
