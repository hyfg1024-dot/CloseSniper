#!/usr/bin/env python3
from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path


LABEL = "com.panpanc.daily-validation"


def main() -> None:
    project = Path(sys.argv[1]).resolve()
    python = project / ".venv" / "bin" / "python"
    validator = project / "validate.py"
    if not python.exists():
        raise SystemExit("请先双击启动尾盘雷达，完成运行环境安装。")

    agent_dir = Path.home() / "Library" / "LaunchAgents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    plist_path = agent_dir / f"{LABEL}.plist"
    intervals = [
        {"Weekday": weekday, "Hour": 9, "Minute": 45}
        for weekday in range(2, 7)
    ]
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(python), str(validator)],
        "WorkingDirectory": str(project),
        "StartCalendarInterval": intervals,
        "StandardOutPath": "/tmp/panpanc-validation.log",
        "StandardErrorPath": "/tmp/panpanc-validation-error.log",
        "ProcessType": "Background",
    }
    with plist_path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)

    domain = f"gui/{subprocess.check_output(['id', '-u'], text=True).strip()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    subprocess.run(["launchctl", "enable", f"{domain}/{LABEL}"], check=True)
    print(f"已安装：工作日 09:45 自动校验（{plist_path}）")


if __name__ == "__main__":
    main()

