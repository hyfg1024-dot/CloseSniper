from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
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
SETTINGS_PATH = APP_SUPPORT_ROOT / "config" / "ai_observer.json"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
MAX_OBSERVATIONS = 5


@dataclass(frozen=True)
class AIObserverSettings:
    api_key: str = ""
    enabled: bool = False
    model: str = "deepseek-chat"

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


def load_settings(path: Path = SETTINGS_PATH) -> AIObserverSettings:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AIObserverSettings()
    return AIObserverSettings(
        api_key=str(data.get("api_key") or "").strip(),
        enabled=bool(data.get("enabled", False)),
        model=str(data.get("model") or "deepseek-chat").strip() or "deepseek-chat",
    )


def save_settings(settings: AIObserverSettings, path: Path = SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


SYSTEM_PROMPT = """你是A股尾盘观察助手。只能根据输入的结构化免费行情事实解释风险，禁止编造新闻、主力资金、盘口或未提供的数据；禁止预测明日涨跌、给出买卖或仓位建议。
输出严格 JSON：
{
  "verdict":"继续观察|谨慎观察|放弃观察",
  "strengths":["最多3条，每条必须含输入中的数字或明确字段"],
  "risks":["最多2条，每条必须基于输入事实或数据缺失"],
  "confirm_before_1452":["最多2条、可由14:52数据核验的条件"]
}
如果事实不足，结论必须是“谨慎观察”。"""


def _clean(value: Any) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value][:8]
    return str(value)


def build_context(two_stage: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Compact facts already obtained during the 14:45 free-market scan."""
    fields = (
        "change_pct", "volume_ratio", "turnover", "float_cap_yi", "price", "vwap_ratio",
        "pullback_pct", "market_intraday_pct", "rank_reason", "hot_concepts", "ma_bull",
        "vwap_strong", "relative_strong", "volume_step", "failed_reasons",
    )
    current_facts = {key: _clean(current.get(key)) for key in fields if key in current}
    return {
        "stock": {"code": str(two_stage.get("code", "")), "name": str(two_stage.get("name", ""))},
        "strict_two_stage": {
            "score_1430": _clean(two_stage.get("score_1430")),
            "score_1445": _clean(two_stage.get("score_1445")),
            "watch_score": _clean(two_stage.get("watch_score")),
            "entry_price_1445": _clean(two_stage.get("entry_price")),
        },
        "free_market_facts_1445": current_facts,
        "data_limit": "仅使用14:45前已获取的免费行情与题材字段；没有逐笔、大单或新闻数据。",
    }


def _parse_reply(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "verdict": "谨慎观察",
            "strengths": [],
            "risks": ["AI 返回格式异常，不能将其作为观察依据。"],
            "confirm_before_1452": ["等待严格标准第三次扫描确认。"],
        }
    verdict = str(data.get("verdict") or "谨慎观察")
    if verdict not in {"继续观察", "谨慎观察", "放弃观察"}:
        verdict = "谨慎观察"
    return {
        "verdict": verdict,
        "strengths": [str(item) for item in data.get("strengths", [])[:3]],
        "risks": [str(item) for item in data.get("risks", [])[:2]],
        "confirm_before_1452": [str(item) for item in data.get("confirm_before_1452", [])[:2]],
    }


def analyze_one(context: dict[str, Any], settings: AIObserverSettings) -> dict[str, Any]:
    response = requests.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
        json={
            "model": settings.model,
            "temperature": 0.1,
            "max_tokens": 420,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))},
            ],
        },
        timeout=(4, 14),
    )
    response.raise_for_status()
    payload = response.json()
    content = str(payload["choices"][0]["message"]["content"] or "")
    parsed = _parse_reply(content)
    return {**parsed, "raw": content, "model": settings.model, "generated_at": datetime.now().isoformat(timespec="seconds")}


def analyze_watchlist(
    candidates: pd.DataFrame | Iterable[dict[str, Any]],
    current_rows: Iterable[dict[str, Any]],
    settings: AIObserverSettings | None = None,
) -> dict[str, dict[str, Any]]:
    """Return quickly: AI analysis is optional and must never delay the 14:52 job."""
    config = settings or load_settings()
    if not config.enabled or not config.configured:
        return {}
    current_map = {str(row.get("code", "")).zfill(6): row for row in current_rows}
    records = candidates.to_dict("records") if isinstance(candidates, pd.DataFrame) else list(candidates)
    contexts = [
        build_context(item, current_map.get(str(item.get("code", "")).zfill(6), {}))
        for item in records[:MAX_OBSERVATIONS]
    ]
    if not contexts:
        return {}
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(3, len(contexts))) as pool:
        futures = {pool.submit(analyze_one, context, config): context for context in contexts}
        for future in as_completed(futures):
            context = futures[future]
            code = str(context["stock"]["code"]).zfill(6)
            try:
                results[code] = future.result()
            except Exception as exc:
                results[code] = {
                    "verdict": "谨慎观察",
                    "strengths": [],
                    "risks": [f"AI 解读暂不可用：{type(exc).__name__}"],
                    "confirm_before_1452": ["等待严格标准第三次扫描确认。"],
                    "raw": "",
                    "model": config.model,
                    "error": str(exc)[:240],
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                }
    return results
