"""Per-person index of recent feed items, written into each guru feed as person_index.

It exists so the report writer does not have to scan every record to know who has new material:
any person with at least one non-noise entry newer than the report's checkpoint has new material.
The index is a finding aid only. It carries no grades of its own beyond what the record already says,
and it is never evidence.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

LOOKBACK_HOURS = 72
URL_RE = re.compile(r"https?://\S+")
WORDLE_RE = re.compile(r"^\s*(wordle|connections|strands)\b[^\n]*\d", re.I)
# everything that is not a letter or digit in any script
NONWORD_RE = re.compile(r"[\W_]+", re.UNICODE)

NOTE = ("Finding aid only, never evidence. Entries newer than the run's checkpoint (by the clock named here) are "
        "this run's new material. A person with at least one entry where noise_hint is false after the checkpoint "
        "must not be listed under '새 발언 없음'; read the full record before writing about it. noise_hint=true "
        "marks only obvious noise (games, bare reactions with nothing quoted); everything else still needs judging. "
        "If the checkpoint is older than lookback_hours, scan the full feed instead.")


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _preview(text: str | None, n: int = 100) -> str:
    t = " ".join(URL_RE.sub("", text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def x_noise(post: dict) -> bool:
    text = post.get("text") or ""
    if WORDLE_RE.search(text):
        return True
    core = NONWORD_RE.sub("", URL_RE.sub("", text))
    if len(core) < 4 and not post.get("quoted_post"):
        return True
    return False


def build(entries: list[dict], generated_at: str, clock: str) -> dict:
    """entries: dicts with person, role, at, url, kind, preview, noise_hint (+ optional grade, published_at)."""
    gen = _parse(generated_at)
    lo = gen - timedelta(hours=LOOKBACK_HOURS) if gen else None
    people: dict[str, dict] = {}
    for e in entries:
        at = _parse(e.get("at"))
        if not at or (lo and at < lo) or (gen and at > gen):
            continue
        p = people.setdefault(e["person"], {"role": e.get("role") or "core", "count": 0, "non_noise": 0, "items": []})
        p["count"] += 1
        p["non_noise"] += 0 if e.get("noise_hint") else 1
        item = {k: v for k, v in e.items() if k not in ("person", "role") and v not in (None, "")}
        item["at"] = at.astimezone(KST).isoformat(timespec="seconds")
        p["items"].append(item)
    for p in people.values():
        p["items"].sort(key=lambda i: i["at"])
    return {"clock": clock, "lookback_hours": LOOKBACK_HOURS, "note": NOTE,
            "people": dict(sorted(people.items()))}


def for_x(posts: list[dict], generated_at: str) -> dict:
    entries = []
    for p in posts:
        q = p.get("quoted_post") or {}
        entries.append({
            "person": p.get("person"), "role": p.get("role"), "at": p.get("created_at"), "url": p.get("url"),
            "kind": "quote_post" if q else ("reply" if p.get("is_self_reply") else "post"),
            "preview": _preview(p.get("text")),
            "quoted_author": q.get("author") if q else None,
            "noise_hint": x_noise(p),
        })
    return build(entries, generated_at, "created_at")


def _norm(t: str) -> str:
    return " ".join(NONWORD_RE.sub(" ", (t or "").lower()).split())


def same_interview_groups(videos: list[dict]) -> dict[str, list[str]]:
    """video_id -> other video_ids of the same person sharing at least 2 near-identical quotes (ratio >= 0.85)."""
    qs = {v["video_id"]: [_norm(q.get("text")) for q in v.get("quotes") or [] if len(q.get("text") or "") >= 40]
          for v in videos if v.get("video_id")}
    out: dict[str, list[str]] = {}
    vs = [v for v in videos if qs.get(v.get("video_id"))]
    for i, a in enumerate(vs):
        for b in vs[i + 1:]:
            if a.get("person") != b.get("person"):
                continue
            hits = sum(1 for x in qs[a["video_id"]]
                       if any(SequenceMatcher(None, x, y).ratio() >= 0.85 for y in qs[b["video_id"]]))
            if hits >= 2:
                out.setdefault(a["video_id"], []).append(b["video_id"])
                out.setdefault(b["video_id"], []).append(a["video_id"])
    return out


def for_yt(videos: list[dict], generated_at: str) -> dict:
    groups = same_interview_groups(videos)
    pub = {v.get("video_id"): v.get("published_at") or "" for v in videos}
    entries = []
    for v in videos:
        if not v.get("appearance_confirmed"):
            continue
        same = groups.get(v.get("video_id")) or []
        entries.append({
            "person": v.get("person"), "role": v.get("role"),
            "at": v.get("evidence_ready_at") or v.get("first_seen_at"),
            "url": v.get("url"), "kind": "video", "preview": _preview(v.get("title")),
            "grade": v.get("evidence_grade"), "published_at": v.get("published_at"),
            "same_interview_as": same or None,
            "earliest_published_at": min([pub[x] for x in same if pub.get(x)] + [v.get("published_at") or ""]) if same else None,
            "noise_hint": False,
        })
    idx = build(entries, generated_at, "evidence_ready_at (first_seen_at when no usable quotes yet)")
    idx["same_interview_note"] = ("same_interview_as lists other videos of the same person that share at least two "
                                  "near-identical quotes: treat them as one interview (CROSS-UPLOAD INTERVIEW DEDUPE), "
                                  "dated by earliest_published_at.")
    return idx


def for_web(items: list[dict], generated_at: str) -> dict:
    entries = []
    for i in items:
        if i.get("gemini_status") == "not_about_person":
            continue
        entries.append({
            "person": i.get("person"), "role": i.get("role"),
            "at": i.get("evidence_ready_at") or i.get("discovered_at"),
            "url": i.get("canonical_url"), "kind": i.get("source_kind"), "preview": _preview(i.get("title")),
            "grade": i.get("evidence_grade"), "published_at": i.get("published_at"),
            "noise_hint": False,
        })
    return build(entries, generated_at, "evidence_ready_at (discovered_at when not graded)")
