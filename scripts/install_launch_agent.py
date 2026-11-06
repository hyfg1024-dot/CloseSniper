#!/usr/bin/env python3
from __future__ import annotations

import plistlib
import json
from datetime import datetime
import shutil
import subprocess
import sys
from pathlib import Path


VALIDATION_LABEL = "com.closesniper.daily-validation"
SCAN_SLOTS = {"1430": (14, 30), "1445": (14, 45), "1452": (14, 52)}
REVIEW_LABEL = "com.closesniper.full-review"
LAUNCHD_WEEKDAYS = range(1, 6)  # launchd: Sunday=0/7, Monday=1, Friday=5
LEGACY_LABELS = [
    "com.panpanc.daily-validation",
    "com.panpanc.tail-scan-1430",
    "com.panpanc.tail-scan-1445",
    "com.panpanc.tail-scan-1452",
]


def install_agent(domain: str, plist_path: Path, label: str, payload: dict) -> None:
    with plist_path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    subprocess.run(["launchctl", "enable", f"{domain}/{label}"], check=True)


def weekday_intervals(hour: int, minute: int) -> list[dict[str, int]]:
    return [
        {"Weekday": weekday, "Hour": hour, "Minute": minute}
        for weekday in LAUNCHD_WEEKDAYS
    ]


def update_home_registration(project: Path) -> None:
    registry = Path.home() / "Applications" / "12Agent" / "programs.json"
    if not registry.exists():
        return
    programs = json.loads(registry.read_text(encoding="utf-8"))
    for entry in programs:
        if entry.get("id") == "close-sniper" and Path(entry.get("cwd", "")).resolve() == project:
            entry["schedule"] = "工作日 09:45 / 10:01 / 10:30 校验；14:30 / 14:45 采样；14:52 严格限时终判；14:54 独立对照"
            labels = entry.setdefault("schedule_labels", [])
            if REVIEW_LABEL not in labels:
                labels.append(REVIEW_LABEL)
            backup = registry.with_name(f"programs.before-closesniper-deadline-{datetime.now():%Y%m%d%H%M%S%f}.json")
            shutil.copy2(registry, backup)
            temporary = registry.with_suffix(".closesniper.tmp")
            temporary.write_text(json.dumps(programs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(registry)
            break


def main() -> None:
    project = Path(sys.argv[1]).resolve()
    source_python = project / ".venv" / "bin" / "python"
    if not source_python.exists():
        raise SystemExit("请先双击尾盘狙击 CloseSniper，完成运行环境安装。")

    install_dir = Path.home() / "Library" / "Application Support" / "CloseSniper"
    data_dir = install_dir / "data"
    log_dir = install_dir / "logs"
    install_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            "rsync", "-a", "--delete",
            "--exclude", ".git/", "--exclude", "data/", "--exclude", "logs/", "--exclude", "config/",
            "--exclude", "__pycache__/", "--exclude", ".pytest_cache/",
            f"{project}/", f"{install_dir}/",
        ],
        check=True,
    )
    legacy_db = project / "data" / "panpanc.db"
    deployed_db = data_dir / "closesniper.db"
    if not deployed_db.exists() and legacy_db.exists():
        shutil.copy2(legacy_db, deployed_db)

    python = install_dir / ".venv" / "bin" / "python"
    validator = install_dir / "validate.py"
    scanner = install_dir / "auto_scan.py"
    if not python.exists():
        raise SystemExit("后台运行环境部署失败：未找到Application Support中的Python。")

    agent_dir = Path.home() / "Library" / "LaunchAgents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{subprocess.check_output(['id', '-u'], text=True).strip()}"
    for legacy_label in LEGACY_LABELS:
        legacy_path = agent_dir / f"{legacy_label}.plist"
        subprocess.run(
            ["launchctl", "bootout", domain, str(legacy_path)],
            check=False,
            capture_output=True,
        )
        legacy_path.unlink(missing_ok=True)

    plist_path = agent_dir / f"{VALIDATION_LABEL}.plist"
    intervals = weekday_intervals(9, 45) + weekday_intervals(10, 1) + weekday_intervals(10, 30)
    payload = {
        "Label": VALIDATION_LABEL,
        "ProgramArguments": [str(python), str(validator)],
        "WorkingDirectory": str(install_dir),
        "EnvironmentVariables": {"CLOSESNIPER_HOME": str(install_dir), "PYTHONUNBUFFERED": "1"},
        "StartCalendarInterval": intervals,
        "StandardOutPath": str(log_dir / "validation.log"),
        "StandardErrorPath": str(log_dir / "validation-error.log"),
        "ProcessType": "Background",
    }
    install_agent(domain, plist_path, VALIDATION_LABEL, payload)

    for slot, (hour, minute) in SCAN_SLOTS.items():
        label = f"com.closesniper.tail-scan-{slot}"
        scan_plist = agent_dir / f"{label}.plist"
        scan_payload = {
            "Label": label,
            "ProgramArguments": [str(python), str(scanner), "--slot", slot],
            "WorkingDirectory": str(install_dir),
            "EnvironmentVariables": {"CLOSESNIPER_HOME": str(install_dir), "PYTHONUNBUFFERED": "1"},
            "StartCalendarInterval": weekday_intervals(hour, minute),
            "StandardOutPath": str(log_dir / f"scan-{slot}.log"),
            "StandardErrorPath": str(log_dir / f"scan-{slot}-error.log"),
            "ProcessType": "Background",
        }
        install_agent(domain, scan_plist, label, scan_payload)

    review_payload = {
        **scan_payload,
        "Label": REVIEW_LABEL,
        "ProgramArguments": [str(python), str(scanner), "--review"],
        "StartCalendarInterval": weekday_intervals(14, 54),
        "StandardOutPath": str(log_dir / "review.log"),
        "StandardErrorPath": str(log_dir / "review-error.log"),
    }
    install_agent(domain, agent_dir / f"{REVIEW_LABEL}.plist", REVIEW_LABEL, review_payload)
    update_home_registration(project)

    print(f"后台环境：{install_dir}")
    print("已安装：14:30/14:45采样限时150秒；14:52严格终判，14:52:40计算截止；14:54独立全市场对照；09:45/10:01/10:30校验。")


if __name__ == "__main__":
    main()
