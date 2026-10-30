from __future__ import annotations

import json
from typing import Any

from ..db import all_, execute

DEFAULTS: dict[str, Any] = {
    "chunkSeconds": 30,
    "autoSummarize": True,
    "summaryProviderId": None,
    "summaryTemplate": "meeting",
    "summaryPrompt": "",
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
