#!/usr/bin/env python3
from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path


VALIDATION_LABEL = "com.panpanc.daily-validation"
SCAN_SLOTS = {"1430": (14, 30), "1445": (14, 45), "1452": (14, 52)}


def install_agent(domain: str, plist_path: Path, label: str, payload: dict) -> None:
    with plist_path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    subprocess.run(["launchctl", "enable", f"{domain}/{label}"], check=True)


def main() -> None:
    project = Path(sys.argv[1]).resolve()
    python = project / ".venv" / "bin" / "python"
    validator = project / "validate.py"
    scanner = project / "auto_scan.py"
    if not python.exists():
        raise SystemExit("请先双击启动尾盘雷达，完成运行环境安装。")

    agent_dir = Path.home() / "Library" / "LaunchAgents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    plist_path = agent_dir / f"{VALIDATION_LABEL}.plist"
    intervals = [
        {"Weekday": weekday, "Hour": hour, "Minute": minute}
        for weekday in range(2, 7)
        for hour, minute in ((9, 45), (10, 30))
    ]
    payload = {
        "Label": VALIDATION_LABEL,
        "ProgramArguments": [str(python), str(validator)],
        "WorkingDirectory": str(project),
        "StartCalendarInterval": intervals,
        "StandardOutPath": "/tmp/panpanc-validation.log",
        "StandardErrorPath": "/tmp/panpanc-validation-error.log",
        "ProcessType": "Background",
    }
    domain = f"gui/{subprocess.check_output(['id', '-u'], text=True).strip()}"
    install_agent(domain, plist_path, VALIDATION_LABEL, payload)

    for slot, (hour, minute) in SCAN_SLOTS.items():
        label = f"com.panpanc.tail-scan-{slot}"
        scan_plist = agent_dir / f"{label}.plist"
        scan_payload = {
            "Label": label,
            "ProgramArguments": [str(python), str(scanner), "--slot", slot],
            "WorkingDirectory": str(project),
            "StartCalendarInterval": [
                {"Weekday": weekday, "Hour": hour, "Minute": minute}
                for weekday in range(2, 7)
            ],
            "StandardOutPath": f"/tmp/panpanc-scan-{slot}.log",
            "StandardErrorPath": f"/tmp/panpanc-scan-{slot}-error.log",
            "ProcessType": "Background",
        }
        install_agent(domain, scan_plist, label, scan_payload)

    print("已安装：工作日 14:30、14:45、14:52 自动扫描；09:45、10:30 自动校验。")


if __name__ == "__main__":
    main()
