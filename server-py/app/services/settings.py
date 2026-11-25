from __future__ import annotations

import json
from typing import Any

from ..db import all_, execute

DEFAULTS: dict[str, Any] = {
    "chunkSeconds": 120,
    "autoSummarize": True,
    # 本地说话人分离：转写完成后自动标注「说话人 1/2/…」
    "speakerDiarization": False,
    "diarizationThreshold": 0.7,
    # 声纹角色匹配阈值：余弦相似度低于它就只显示「说话人 N」，不硬套名字
    "voiceprintThreshold": 0.6,
    "summaryProviderId": None,
    "summaryTemplate": "meeting",
    # 单条录音的自定义要求（沿用老字段名，老数据不用迁移）
    "summaryPrompt": "",
    # 日报 / 周报 / 月报 / 年报 / 任意时段各自的自定义要求
    "promptDay": "",
    "promptWeek": "",
    "promptMonth": "",
    "promptYear": "",
    "promptRange": "",
    "language": "zh",
    "theme": "system",
}


def read_settings(user_id: str) -> dict[str, Any]:
    result = dict(DEFAULTS)
    for row in all_("SELECT key, value FROM settings WHERE user_id = ?", (user_id,)):
        try:
            result[row["key"]] = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            result[row["key"]] = row["value"]
    return result


def write_settings(user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if value is None and key not in ("summaryProviderId",):
            continue
        execute(
            """INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)
               ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value""",
            (user_id, key, json.dumps(value, ensure_ascii=False)),
        )
    return read_settings(user_id)
