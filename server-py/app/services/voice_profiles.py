"""声纹角色库：把说话人聚类固化成「我 / 张三 / 李四」，并在每条录音里自动认人。

设计要点：
- 录入时直接复用它已经算好的声纹质心（``recordings.speaker_embeddings``），不重新解码音频；
- 匹配是纯向量余弦，毫秒级；新增 / 删除角色后会把该用户所有已分离录音重新标一遍；
- 「我」是特殊角色（``is_self=1``）：匹配到就记为参与，明显匹配不上就记为未参与，
  落在中间地带记「不确定」，避免只说了两句话时误判。
"""

from __future__ import annotations

import json
import math
import uuid

from ..bus import emit_recording
from ..db import all_, execute, now_iso, one
from .settings import read_settings

SELF_NAME = "我"
MATCH_THRESHOLD = 0.6
# 与「我」的相似度低于这个值才敢下「未参与」的结论，中间算不确定
ABSENT_THRESHOLD = 0.45
MIN_ENROLL_MS = 4_000


def _norm(vec: list[float] | None) -> list[float]:
    if not vec:
        return []
    n = math.sqrt(sum(float(x) * float(x) for x in vec))
    if n <= 0:
        return []
    return [float(x) / n for x in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _loads(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        arr = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return arr if isinstance(arr, list) else []


def profile_public(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "isSelf": bool(row.get("is_self")),
        "sampleCount": int(row.get("sample_count") or 1),
        "sourceRecordingId": row.get("source_recording_id"),
        "sourceSpeaker": row.get("source_speaker"),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def list_profiles(user_id: str) -> list[dict]:
    rows = all_(
        """SELECT * FROM speaker_profiles WHERE user_id = ?
           ORDER BY is_self DESC, created_at ASC""",
        (user_id,),
    )
    return [profile_public(r) for r in rows]


def _profiles(user_id: str) -> list[dict]:
    rows = all_(
        "SELECT * FROM speaker_profiles WHERE user_id = ? ORDER BY is_self DESC",
        (user_id,),
    )
    out = []
    for r in rows:
        emb = _norm(_loads(r.get("embedding")))
        if emb:
            out.append({**r, "_vector": emb})
    return out


def _centroids(rec: dict) -> list[dict]:
    out = []
    for item in _loads(rec.get("speaker_embeddings")):
        vec = _norm(item.get("embedding"))
        if vec:
            out.append(
                {
                    # 对外统一用 1 起的编号，和 segments 里的「说话人 N」一致
                    "speaker": int(item.get("speaker")) + 1,
                    "embedding": vec,
                    "speechMs": int(item.get("speechMs") or 0),
                }
            )
    return out


# ------------------------------------------------------------------ #
# 匹配与回填
# ------------------------------------------------------------------ #
def _match(user_id: str, centroids: list[dict]) -> tuple[list[dict], int | None, float | None]:
    """返回 (聚类→角色, 我是否参与, 与我的最高相似度)。"""
    profiles = _profiles(user_id)
    threshold = float(read_settings(user_id).get("voiceprintThreshold") or MATCH_THRESHOLD)

    # 全局按相似度从高到低贪心：一个角色只认领一个聚类，避免过度分裂的聚类重复命名
    pairs: list[tuple[float, int, dict]] = []
    for c in centroids:
        if c["speechMs"] < MIN_ENROLL_MS:
            continue
        for p in profiles:
            score = _cosine(c["embedding"], p["_vector"])
            if score >= threshold:
                pairs.append((score, c["speaker"], p))
    pairs.sort(key=lambda x: x[0], reverse=True)

    used_speakers: set[int] = set()
    used_profiles: set[str] = set()
    matches: list[dict] = []
    for score, speaker, p in pairs:
        if speaker in used_speakers or p["id"] in used_profiles:
            continue
        used_speakers.add(speaker)
        used_profiles.add(p["id"])
        matches.append(
            {
                "speaker": speaker,
                "profileId": p["id"],
                "name": p["name"],
                "isSelf": bool(p.get("is_self")),
                "score": round(score, 4),
                "speechMs": next(c["speechMs"] for c in centroids if c["speaker"] == speaker),
            }
        )

    me = next((p for p in profiles if p.get("is_self")), None)
    present: int | None = None
    best_self: float | None = None
    if me is not None:
        best_self = max((_cosine(c["embedding"], me["_vector"]) for c in centroids), default=0.0)
        best_self = round(best_self, 4)
        if any(m["isSelf"] for m in matches):
            present = 1
        elif best_self < ABSENT_THRESHOLD:
            present = 0
        else:
            present = None  # 相似度处在灰区：可能只是麦克风变化，先不下结论
    return matches, present, best_self


def _apply_labels(chunks: list[dict], matches: list[dict]) -> None:
    """把角色名写进每个分片的 segments；没有匹配的句子清掉旧名字。"""
    from .transcribe import parse_segments

    by_speaker = {m["speaker"]: m for m in matches}
    for c in chunks:
        segs = parse_segments(c)
        if not segs:
            continue
        for s in segs:
            spk = s.get("speaker")
            m = by_speaker.get(spk)
            if m:
                s["speakerName"] = m["name"]
                s["speakerIsSelf"] = bool(m["isSelf"])
            else:
                s.pop("speakerName", None)
                s.pop("speakerIsSelf", None)
        execute(
            "UPDATE chunks SET segments = ?, updated_at = ? WHERE id = ?",
            (json.dumps(segs, ensure_ascii=False), now_iso(), c["id"]),
        )


def apply_recording(recording_id: str, user_id: str) -> dict:
    """用库里已有的质心重新匹配角色并回填，不重跑推理。"""
    rec = one(
        "SELECT * FROM recordings WHERE id = ? AND user_id = ?",
        (recording_id, user_id),
    )
    if not rec:
        return {}
    centroids = _centroids(rec)
    matches, present, similarity = _match(user_id, centroids)
    chunks = all_(
        "SELECT * FROM chunks WHERE recording_id = ? ORDER BY seq ASC",
        (recording_id,),
    )
    _apply_labels(chunks, matches)
    execute(
        """UPDATE recordings
           SET speaker_matches = ?, user_present = ?, user_similarity = ?
           WHERE id = ?""",
        (json.dumps(matches, ensure_ascii=False), present, similarity, recording_id),
    )
    emit_recording(recording_id, "recording", {"speakerMatches": matches, "userPresent": present})
    return {"speakers": matches, "userPresent": present, "userSimilarity": similarity}


# ------------------------------------------------------------------ #
# 录入 / 删除
# ------------------------------------------------------------------ #
def enroll_from_recording(
    user_id: str,
    recording_id: str,
    speaker: int,
    name: str,
    is_self: bool = False,
) -> dict:
    rec = one(
        "SELECT * FROM recordings WHERE id = ? AND user_id = ?",
        (recording_id, user_id),
    )
    if not rec:
        raise ValueError("录音不存在")

    centroid = next(
        (c for c in _centroids(rec) if c["speaker"] == int(speaker)),
        None,
    )
    if centroid is None:
        raise ValueError("这条录音没有可用的声纹，请重新转写并完成说话人分离后再标记")
    if centroid["speechMs"] < MIN_ENROLL_MS:
        raise ValueError("这个说话人说得太少（不足 4 秒），换一段更长的录音标记会更准")

    name = (name or "").strip()
    if is_self or name == SELF_NAME:
        is_self = True
        name = SELF_NAME
    if not name:
        raise ValueError("角色名不能为空")

    now = now_iso()
    existing: dict | None = None
    if is_self:
        existing = one(
            "SELECT * FROM speaker_profiles WHERE user_id = ? AND is_self = 1",
            (user_id,),
        )
    if not existing:
        existing = one(
            "SELECT * FROM speaker_profiles WHERE user_id = ? AND name = ?",
            (user_id, name),
        )

    if existing:
        old = _norm(_loads(existing.get("embedding")))
        merged = _norm([a + b for a, b in zip(old, centroid["embedding"])]) if old else centroid["embedding"]
        execute(
            """UPDATE speaker_profiles
               SET name = ?, embedding = ?, sample_count = sample_count + 1,
                   is_self = ?, source_recording_id = ?, source_speaker = ?, updated_at = ?
               WHERE id = ?""",
            (
                name,
                json.dumps(merged),
                1 if is_self else 0,
                recording_id,
                int(speaker),
                now,
                existing["id"],
            ),
        )
        profile_id = existing["id"]
    else:
        profile_id = "vp_" + uuid.uuid4().hex[:20]
        execute(
            """INSERT INTO speaker_profiles
               (id, user_id, name, embedding, sample_count, is_self,
                source_recording_id, source_speaker, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
            (
                profile_id,
                user_id,
                name,
                json.dumps(centroid["embedding"]),
                1 if is_self else 0,
                recording_id,
                int(speaker),
                now,
                now,
            ),
        )

    # 新角色立即对全部已分离录音生效（包括当前这条），不用等下次转写
    _rematch_all(user_id)
    profile = one("SELECT * FROM speaker_profiles WHERE id = ?", (profile_id,))
    return profile_public(profile) if profile else {}


def delete_profile(user_id: str, profile_id: str) -> bool:
    row = one(
        "SELECT * FROM speaker_profiles WHERE id = ? AND user_id = ?",
        (profile_id, user_id),
    )
    if not row:
        return False
    execute("DELETE FROM speaker_profiles WHERE id = ?", (profile_id,))
    _rematch_all(user_id, dropped_profile_id=profile_id, dropped_is_self=bool(row.get("is_self")))
    return True


def _rematch_all(
    user_id: str,
    dropped_profile_id: str | None = None,
    dropped_is_self: bool = False,
) -> None:
    """把该用户所有已分离录音重新匹配一遍。

    删除角色时没有质心可比，只需要把旧名字从缓存里剔掉；其余情况直接重算。
    """
    rows = all_(
        """SELECT * FROM recordings
           WHERE user_id = ? AND diarized_at IS NOT NULL
             AND speaker_embeddings IS NOT NULL""",
        (user_id,),
    )
    for rec in rows:
        if dropped_profile_id:
            matches = [
                m for m in _loads(rec.get("speaker_matches"))
                if m.get("profileId") != dropped_profile_id
            ]
            chunks = all_(
                "SELECT * FROM chunks WHERE recording_id = ? ORDER BY seq ASC",
                (rec["id"],),
            )
            _apply_labels(chunks, matches)
            present = None if dropped_is_self else rec.get("user_present")
            similarity = None if dropped_is_self else rec.get("user_similarity")
            execute(
                """UPDATE recordings
                   SET speaker_matches = ?, user_present = ?, user_similarity = ?
                   WHERE id = ?""",
                (json.dumps(matches, ensure_ascii=False), present, similarity, rec["id"]),
            )
            emit_recording(rec["id"], "recording", {"speakerMatches": matches, "userPresent": present})
        else:
            apply_recording(rec["id"], user_id)
