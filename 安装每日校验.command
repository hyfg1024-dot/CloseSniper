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
osascript -e 'display dialog "CloseSniper后台任务已安装：工作日14:30、14:45静默采样，14:52生成严格标准和改进流程两套结果；09:45和10:30自动校验。Mac需保持开机且未休眠。" buttons {"好"} default button "好" with icon note'
