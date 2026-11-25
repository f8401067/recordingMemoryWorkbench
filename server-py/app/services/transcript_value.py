"""判断一段转写是不是"没有实际价值"。

用途：空录 / 只录到环境噪声 / ASR 对着音乐吐套话时，转写结果常常是一句
「感谢观看」「字幕由 xxx 提供」「[音乐]」。这类记录混在当天列表里会把真内容挤下去，
所以在转写完成后打一个 `low_value` 标记，前端把它排到最后并默认收起。

设计原则：**宁可放过，不可误杀**。只有下面几种一眼就能确定的情况才判 1：

1. 没有 sentence segments，或去掉时间戳后一个字都没有 —— 没有人声；
2. 去掉标点/空白/方括号标记后，有效字符少得可怜（默认 < 6）—— 零星几个字；
3. 整段几乎全落在「ASR 噪声套话」模板里（音乐、掌声、字幕署名、关注点赞）；
4. 去重之后只剩极少数几种字符，Vocabulary 低到不可能是一句话；
5. 中文 / 英数字符占比过低（整段基本是标点或单字节符号）。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 最小有效字符数：低于这个值基本不可能是有意义的一句话
MIN_CHARS = 6

# 去重后的字符种类：低于这个数说明是"啊啊啊""嗯嗯嗯"这类
MIN_UNIQUE_CHARS = 4

# 中文/英数字符占比下限：低于它就是一堆标点
MIN_MEANING_RATIO = 0.3

# AI 判定"只有零碎文字、拼不出实际话题"时落库的原因（前端折叠区直接展示）
NO_TOPIC_REASON = "只有零碎文字，没有实际话题"

_BRACKET = re.compile(r"[\[\(（【][^\]\)）】]{0,12}[\]\)）】]")
_PUNCT = re.compile(
    r"[\s　,.!?;:'\"`~^_\-=+*/\\|<>，。！？；：、'\"“”‘’（）()\[\]【】《》…—·　·…]+"
)
# 有信息量的字符：汉字 / 假名 / 谚文 / 拉丁字母 / 数字
_MEANING = re.compile(r"[0-9A-Za-z一-鿿぀-ヿ가-힯]")

# ASR 对着纯音乐、掌声、静音时常吐出来的东西
_NOISE_PATTERNS: list[tuple[str, str]] = [
    (r"^\[?(音乐|背景音乐|纯音乐|music)\]?$", "疑似只有背景音乐"),
    (r"^\[?(掌声|applause|笑声|laughter|噪音|噪声|杂音)\]?$", "疑似只有环境噪声"),
    (r"感谢(观看|收看|聆听)|谢谢观看|thanks for watching|thank you for watching", "疑似视频片尾套话"),
    (r"字幕(由|来自|提供|组)|subtitles?\s*by|amara\.org|同步(字幕|音轨)", "疑似字幕署名"),
    (r"(请)?不吝?(点赞|关注|订阅|投币)|点赞关注|三连|一键三连|记得订阅|欢迎关注", "疑似视频互动话术"),
    (r"^(嗯|啊|哦|呃|诶|喂|喂喂|砰|嘭|咚|hello|hi)+$", "疑似只有语气词"),
    (r"^(您好|你好)?\s*(?:请问有什么可以帮(您|你)|有什么可以帮(您|你))\s*[？?]?$", "疑似客服套话"),
    (r"^[0-9\s.,。-]+$", "疑似只有数字"),
]

_NOISE_TEXT_LIMIT = 3  # 命中模板后剩余文本不超过这么长就判无意义

# 命中这些原因说明整段确实没有人声（空转写、纯音乐、环境噪声、只有语气词）。
# 设备常驻监听到的多是这类杂音，可以直接丢弃；而"只有 N 个有效字"可能只是
# 话说得太短，仍留在低价值折叠区让人自己判断，不自动删。
AUTO_DISCARD_REASONS = frozenset(
    {
        "没有识别到人声",
        "整段没有可读文字",
        "疑似只有背景音乐",
        "疑似只有环境噪声",
        "疑似只有语气词",
    }
)


def should_auto_discard(low: bool, reason: str | None) -> bool:
    """是否整段确实没有人声、可以自动丢弃。"""
    return bool(low) and reason in AUTO_DISCARD_REASONS


def _clean(text: str) -> str:
    """去掉方括号标记和所有标点/空白，留下"有效字符"。"""
    s = _BRACKET.sub("", text or "")
    s = _PUNCT.sub("", s)
    return s.strip()


def segment_texts(segments: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for s in segments or []:
        t = str((s or {}).get("text") or "").strip()
        if t:
            out.append(t)
    return out


def _noise_hit(text: str) -> str | None:
    for pattern, reason in _NOISE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return reason
    return None


def evaluate_transcript(segments: list[dict[str, Any]] | None) -> tuple[bool, str | None]:
    """返回 (是否低价值, 原因)。低价值=没有人声或几乎没有实际内容。"""
    texts = segment_texts(segments or [])
    joined = " ".join(texts).strip()

    if not joined:
        return True, "没有识别到人声"

    clean = _clean(joined)

    # 1) 连方括号标记都不剩 —— 整段是 [音乐] 之类
    if not clean:
        return True, "整段没有可读文字"

    # 2) 噪声套话模板：命中且套话之外的剩余内容很短。
    #    放在字数判断之前，这样「嗯。」「啊。砰！」这类只有语气词的短录音
    #    会得到明确的"疑似只有语气词"，而不是模棱两可的"只有 N 个有效字"，
    #    自动丢弃才能安全地只针对这一类。
    reason = _noise_hit(clean)
    if reason:
        return True, reason

    # 3) 有效字符太少
    if len(clean) < MIN_CHARS:
        return True, f"只有 {len(clean)} 个有效字"

    # 4) 字符种类少到不构成一句话
    if len(set(clean)) < MIN_UNIQUE_CHARS and len(clean) < 20:
        return True, "内容重复，没有实际语义"

    # 5) 有意义字符（汉字/英数）占比过低
    meaningful = len(_MEANING.findall(clean))
    if meaningful / len(clean) < MIN_MEANING_RATIO:
        return True, "几乎没有可阅读的文字"

    return False, None


def flag_recording(recording_id: str) -> tuple[bool, str | None]:
    """算出某条录音的价值标记并落库。返回 (是否低价值, 原因)。"""
    from ..db import execute  # 延迟导入：避免和处理 Job 的模块互相套住

    segments = transcript_segments(recording_id)
    low, reason = evaluate_transcript(segments)
    execute(
        "UPDATE recordings SET low_value = ?, low_reason = ?, value_checked = 1 WHERE id = ?",
        (1 if low else 0, reason, recording_id),
    )
    return low, reason


def transcript_segments(recording_id: str) -> list[dict[str, Any]]:
    from ..db import all_  # 同上

    rows = all_("SELECT * FROM chunks WHERE recording_id = ? ORDER BY seq ASC", (recording_id,))
    out: list[dict[str, Any]] = []
    for c in rows:
        try:
            arr = json.loads(c.get("segments") or "")
        except (TypeError, ValueError):
            arr = []
        if isinstance(arr, list):
            out.extend(arr)
    out.sort(key=lambda s: s.get("startMs") or 0)
    return out


def mark_no_topic(recording_id: str, reason: str = NO_TOPIC_REASON) -> bool:
    """把 AI 判定"没有实际话题"的录音并进低价值组。返回是否真的改动了。"""
    from ..db import execute  # 延迟导入：避免和处理 Job 的模块互相套住

    cur = execute(
        """UPDATE recordings SET low_value = 1, low_reason = ?, value_checked = 1
           WHERE id = ? AND COALESCE(low_value, 0) = 0""",
        (reason, recording_id),
    )
    return bool(getattr(cur, "rowcount", 0))


def clear_no_topic(recording_id: str) -> bool:
    """重新整理后 AI 认为有实际话题时，撤销之前那条 AI 判定（不动规则判定的低价值）。"""
    from ..db import execute

    cur = execute(
        """UPDATE recordings SET low_value = 0, low_reason = NULL
           WHERE id = ? AND low_reason = ?""",
        (recording_id, NO_TOPIC_REASON),
    )
    return bool(getattr(cur, "rowcount", 0))


def backfill_no_topic(limit: int = 1000) -> int:
    """历史纪要里已经被 AI 写成「无有效内容」的录音，补打低价值标记。"""
    from ..db import all_

    rows = all_(
        """SELECT s.recording_id FROM summaries s
           JOIN recordings r ON r.id = s.recording_id
           WHERE s.scope = 'recording' AND s.status = 'ready'
             AND COALESCE(r.low_value, 0) = 0
             AND s.title LIKE ?
           ORDER BY s.created_at DESC LIMIT ?""",
        ("无有效内容%", limit),
    )
    n = 0
    for r in rows:
        if r.get("recording_id") and mark_no_topic(r["recording_id"]):
            n += 1
    return n


def backfill_unchecked(limit: int = 1000) -> int:
    """给历史录音补算价值标记（有没有这套逻辑之前建的数据）。

    只处理已经处理完的（ready/failed），录音中/转写中的不动，避免半截数据被误判。
    """
    from ..db import all_

    rows = all_(
        """SELECT id FROM recordings
           WHERE COALESCE(value_checked, 0) = 0 AND status IN ('ready', 'failed')
           ORDER BY started_at DESC LIMIT ?""",
        (limit,),
    )
    for r in rows:
        try:
            flag_recording(r["id"])
        except Exception as e:  # noqa: BLE001 - 补算失败不该拖住启动
            print(f"[value] 补算 {r['id']} 失败：{e}", flush=True)
    return len(rows)
