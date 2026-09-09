from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests


APP_SUPPORT_ROOT = Path(
    os.getenv(
        "CLOSESNIPER_HOME",
        str(Path.home() / "Library" / "Application Support" / "CloseSniper"),
    )
)
DEFAULT_SETTINGS_PATH = APP_SUPPORT_ROOT / "config" / "telegram.json"
TELEGRAM_API_ROOT = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str = ""
    chat_id: str = ""
    bot_username: str = ""
    enabled: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)


def load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> TelegramSettings:
    if not path.exists():
        return TelegramSettings()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return TelegramSettings()
    return TelegramSettings(
        bot_token=str(payload.get("bot_token", "")).strip(),
        chat_id=str(payload.get("chat_id", "")).strip(),
        bot_username=str(payload.get("bot_username", "")).strip(),
        enabled=bool(payload.get("enabled", False)),
    )


def save_settings(settings: TelegramSettings, path: Path = DEFAULT_SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def _api_request(
    token: str,
    method: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: int = 15,
) -> Any:
    try:
        if payload is None:
            response = requests.get(f"{TELEGRAM_API_ROOT}/bot{token}/{method}", timeout=timeout)
        else:
            response = requests.post(
                f"{TELEGRAM_API_ROOT}/bot{token}/{method}",
                json=payload,
                timeout=timeout,
            )
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise TelegramError("无法连接 Telegram，请检查网络后重试。") from exc
    if not response.ok or not data.get("ok"):
        description = str(data.get("description", "Telegram接口返回错误"))
        raise TelegramError(description)
    return data.get("result")


def bot_identity(token: str) -> str:
    result = _api_request(token, "getMe")
    return str(result.get("username", ""))


def discover_chat_id(token: str) -> str:
    updates = _api_request(token, "getUpdates", payload={"limit": 100, "timeout": 0})
    chats: list[dict[str, Any]] = []
    for update in reversed(updates or []):
        message = update.get("message") or update.get("edited_message") or update.get("channel_post")
        if message and message.get("chat"):
            chats.append(message["chat"])
    for chat in chats:
        if chat.get("type") == "private":
            return str(chat["id"])
    if chats:
        return str(chats[0]["id"])
    raise TelegramError("尚未找到聊天。请先向新机器人发送 /start，再点击保存并连接。")


def send_message(settings: TelegramSettings, text: str, *, attempts: int = 3) -> None:
    if not settings.configured:
        raise TelegramError("Telegram尚未完成配置。")
    last_error: TelegramError | None = None
    for attempt in range(attempts):
        try:
            _api_request(
                settings.bot_token,
                "sendMessage",
                payload={
                    "chat_id": settings.chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
            return
        except TelegramError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise last_error or TelegramError("Telegram消息发送失败。")


def _records(items: pd.DataFrame | Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(items, pd.DataFrame):
        return items.to_dict("records")
    return list(items)


def _number(value: Any) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):.1f}"


def format_final_message(
    *,
    trade_date: str,
    strict_candidates: pd.DataFrame | Iterable[dict[str, Any]],
    rational_candidates: pd.DataFrame | Iterable[dict[str, Any]],
    generated_at: datetime | None = None,
) -> str:
    strict = _records(strict_candidates)
    rational = _records(rational_candidates)
    lines = [
        f"🎯 CloseSniper｜{trade_date} 尾盘结果",
        f"生成时间：{(generated_at or datetime.now()):%H:%M:%S}",
        "",
        f"【严格标准｜14:52】{len(strict)}只",
    ]
    if strict:
        for index, item in enumerate(strict, 1):
            price = item.get("price", item.get("entry_price"))
            lines.append(
                f"{index}. {item.get('name', '—')}（{item.get('code', '—')}）"
                f"｜评分{_number(item.get('score'))}｜信号价{_number(price)}"
            )
    else:
        lines.append("无符合条件股票")

    lines.extend(["", f"【改进流程｜三时点加权】{len(rational)}只"])
    if rational:
        for index, item in enumerate(rational, 1):
            lines.append(
                f"{index}. {item.get('name', '—')}（{item.get('code', '—')}）"
                f"｜综合{_number(item.get('composite_score', item.get('score')))}"
                f"｜14:30 {_number(item.get('score_1430'))}"
                f"｜14:45 {_number(item.get('score_1445'))}"
                f"｜14:52 {_number(item.get('score_1452'))}"
                f"｜{item.get('persistence', '—')}"
            )
    else:
        lines.append("无符合条件股票")
    lines.extend(["", "仅供策略研究，不构成投资建议。"])
    return "\n".join(lines)


def format_scan_issue_message(
    *,
    trade_date: str,
    failed_slots: Iterable[str],
    reason: str,
    generated_at: datetime | None = None,
) -> str:
    slots = [str(slot) for slot in failed_slots]
    slot_text = "、".join(f"{slot[:2]}:{slot[2:]}" for slot in slots) or "未知节点"
    concise_reason = str(reason).replace("\n", " ")
    if len(concise_reason) > 700:
        concise_reason = concise_reason[:697] + "…"
    return "\n".join([
        f"⚠️ CloseSniper｜{trade_date} 尾盘扫描未形成最终结果",
        f"通知时间：{(generated_at or datetime.now()):%H:%M:%S}",
        f"异常节点：{slot_text}",
        f"原因：{concise_reason}",
        "系统已自动重试，仍未取得完整行情；今日不生成三时点加权名单。",
        "请勿把“无结果”理解为“扫描成功且无候选”。",
    ])


def format_fast_decision_message(
    *,
    trade_date: str,
    candidates: pd.DataFrame | Iterable[dict[str, Any]],
    generated_at: datetime | None = None,
) -> str:
    rows = _records(candidates)
    lines = [
        f"⚡ CloseSniper｜{trade_date} 14:52冻结决策版",
        f"完成时间：{(generated_at or datetime.now()):%H:%M:%S}",
        "口径：仅复核14:45已入选股票，严格冻结14:52数据。",
        "",
        f"【改进流程｜准时名单】{len(rows)}只",
    ]
    if rows:
        for index, item in enumerate(rows, 1):
            lines.append(
                f"{index}. {item.get('name', '—')}（{item.get('code', '—')}）"
                f"｜综合{_number(item.get('composite_score', item.get('score')))}"
                f"｜{item.get('persistence', '—')}"
            )
    else:
        lines.append("无符合条件股票")
    lines.extend(["", "完整重扫版将在后台完成后另行发送。", "仅供策略研究，不构成投资建议。"])
    return "\n".join(lines)


def format_strict_watch_message(
    *,
    trade_date: str,
    candidates: pd.DataFrame | Iterable[dict[str, Any]],
    analyses: dict[str, dict[str, Any]] | None = None,
    generated_at: datetime | None = None,
) -> str:
    """A deliberately non-actionable early alert before the 14:52 final confirmation."""
    rows = _records(candidates)
    analyses = analyses or {}
    lines = [
        f"👀 CloseSniper｜{trade_date} 严格标准观察提醒",
        f"确认时间：{(generated_at or datetime.now()):%H:%M:%S}",
        "口径：14:30 与 14:45 连续两次符合严格标准。",
        "这只是开始观察，不是最终买入名单；请等待 14:52 第三次确认。",
        "",
        f"【连续两次稳定】{len(rows)}只",
    ]
    if rows:
        for index, item in enumerate(rows, 1):
            analysis = analyses.get(str(item.get("code", "")).zfill(6), {})
            lines.append(
                f"{index}. {item.get('name', '—')}（{item.get('code', '—')}）"
                f"｜观察分 {_number(item.get('watch_score'))}"
                f"｜14:30 {_number(item.get('score_1430'))}"
                f"｜14:45 {_number(item.get('score_1445'))}"
                f"｜现价 {_number(item.get('entry_price', item.get('price')))}"
            )
            if analysis:
                lines.append(f"   AI观察：{analysis.get('verdict', '谨慎观察')}")
                for reason in list(analysis.get("strengths") or [])[:2]:
                    lines.append(f"   · {reason}")
                for risk in list(analysis.get("risks") or [])[:1]:
                    lines.append(f"   风险：{risk}")
                conditions = list(analysis.get("confirm_before_1452") or [])[:1]
                if conditions:
                    lines.append(f"   14:52确认：{conditions[0]}")
    else:
        lines.append("无连续两次符合的股票")
    lines.extend(["", "仅供策略研究，不构成投资建议。"])
    return "\n".join(lines)


def format_review_message(
    *,
    trade_date: str,
    strict_candidates: pd.DataFrame | Iterable[dict[str, Any]],
    improved_candidates: pd.DataFrame | Iterable[dict[str, Any]],
    generated_at: datetime | None = None,
) -> str:
    message = format_final_message(
        trade_date=trade_date,
        strict_candidates=strict_candidates,
        rational_candidates=improved_candidates,
        generated_at=generated_at,
    )
    return (
        message
        .replace("尾盘结果", "完整重扫复核版", 1)
        .replace("【严格标准｜14:52】", "【完整重扫｜严格标准】", 1)
        .replace("【改进流程｜三时点加权】", "【完整重扫｜改进流程】", 1)
    )
