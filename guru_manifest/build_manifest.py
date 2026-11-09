"""Build the 11:00 report manifest: every X / YouTube / guru_web record the morning report must account for.

The collector, not the report writer, decides what is in scope and how many records there are. Each record
gets a fixed id (R001...). The report writer must resolve every id to a source number or to noise; it never
selects records and never counts them.

Window per feed: (previous morning manifest's upper bound, this morning's feed generated_at]. The upper bounds
are the same generated_at values the 11:00 run stores as its checkpoints (AO/AQ/AS), so the manifest window
equals the report window whenever the previous run advanced its checkpoints normally.

Usage: python guru_manifest/build_manifest.py [--force]   (only builds between 10:00 and 10:59 KST unless --force)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import person_index as pi  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
OUT = REPO / "guru_manifest" / "data" / "manifest.json"
CATCHUP = REPO / "guru_manifest" / "data" / "manifest_catchup.json"
MAX_BASES = 3   # main manifest + up to 2 catch-up sets (1-2 missed or unsaved morning runs)
FEEDS = {
    "x": REPO / "guru_x" / "data" / "feed.json",
    "yt": REPO / "guru_yt" / "data" / "youtube_feed.json",
    "web": REPO / "guru_web" / "data" / "web_feed.json",
}
# First build only: the checkpoints the 11/01 11:00 run stored.
SEED_LOWER = {"x": "2026-11-01T10:15:33+09:00", "yt": "2026-11-01T10:10:28+09:00", "web": "2026-11-01T10:05:24+09:00"}
PEOPLE = REPO / "guru_yt" / "channels.json"
FIELDS_NOTE = ("rid is fixed for this manifest. Every rid in records must be resolved in the report: cited by a "
               "source number [n] (several rids may share one [n] when they are the same appearance), or listed "
               "as noise by rid. auto_excluded records are games and bare reactions removed by code; they have no "
               "rid and need no handling. watch_only records (people flagged watch_only, e.g. Ryan Detrick) have no rid either: "
               "they are not shown in the reading section and are checked only for their T2 trigger. "
               "expected_records is the count to print; never recount.")
CATCHUP_NOTE = ("Use only when manifest.json's window lower_exclusive values differ from the stored 11:00 checkpoints "
                "(an earlier 11:00 run was missed or not saved). Pick the one set whose window.x/yt/web.lower_exclusive "
                "equal the checkpoints AO/AQ/AS exactly and use it exactly like manifest.json (its own rids and "
                "expected_records); write set=<id> in the COVERAGE row. If no set matches, the manifest check fails.")


def kst(dt: datetime) -> str:
    return dt.astimezone(KST).isoformat(timespec="seconds")


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def x_records(feed: dict, lo: datetime, hi: datetime) -> tuple[list[dict], list[dict]]:
    recs, auto = [], []
    for p in feed.get("posts", []):
        at = pi._parse(p.get("created_at"))
        if not at or not (lo < at <= hi):
            continue
        q = p.get("quoted_post") or {}
        rc = p.get("reply_context") or []
        kind = "quote_post" if q else ("reply" if rc else "post")
        r = {"feed": "X", "person": p.get("person"), "role": p.get("role") or "core", "tier": p.get("tier"),
             "at": kst(at), "url": p.get("url"), "kind": kind,
             "preview": pi._preview(p.get("text"), 140), "quoted_author": q.get("author") if q else None,
             "in_reply_to": [f"@{c.get('author')}: {pi._preview(c.get('text'), 100)}" for c in rc] or None}
        (auto if pi.x_noise(p) else recs).append(r)
    return recs, auto


def yt_records(feed: dict, lo: datetime, hi: datetime) -> list[dict]:
    groups = pi.same_interview_groups(feed.get("videos", []))
    recs = []
    for v in feed.get("videos", []):
        if not v.get("appearance_confirmed"):
            continue
        at = pi._parse(v.get("evidence_ready_at") or v.get("first_seen_at"))
        if not at or not (lo < at <= hi):
            continue
        recs.append({"feed": "YouTube", "person": v.get("person"), "role": v.get("role") or "core",
                     "tier": v.get("tier"), "at": kst(at), "published_at": v.get("published_at"),
                     "url": v.get("url"), "kind": "video", "channel": v.get("channel"),
                     "grade": v.get("evidence_grade"), "preview": pi._preview(v.get("title"), 140),
                     "_vid": v.get("video_id"), "_same": groups.get(v.get("video_id")) or []})
    return recs


def web_records(feed: dict, lo: datetime, hi: datetime) -> list[dict]:
    recs = []
    for i in feed.get("items", []):
        if i.get("gemini_status") == "not_about_person":
            continue
        at = pi._parse(i.get("evidence_ready_at") or i.get("discovered_at"))
        if not at or not (lo < at <= hi):
            continue
        recs.append({"feed": "web", "person": i.get("person"), "role": i.get("role") or "core",
                     "tier": i.get("tier"), "at": kst(at), "published_at": i.get("published_at"),
                     "url": i.get("canonical_url"), "kind": i.get("source_kind"), "outlet": i.get("outlet"),
                     "grade": i.get("evidence_grade"), "preview": pi._preview(i.get("title"), 140)})
    return recs


def base_lowers(prev: dict, now: datetime) -> list[dict]:
    """Candidate lower bounds, newest first. [0] is the normal one (previous morning's upper); the rest are older
    morning boundaries, kept so a missed or unsaved 11:00 run can still be matched to its stored checkpoints."""
    if not prev:
        return [dict(SEED_LOWER)]
    prev_lower = {k: (prev["window"].get(k) or {}).get("lower_exclusive") for k in FEEDS}
    prev_upper = {k: (prev["window"].get(k) or {}).get("upper") for k in FEEDS}
    older = prev.get("base_lowers") or [prev_lower]
    rebuilt_today = pi._parse(prev["built_at"]).astimezone(KST).date() == now.date()
    chain = older if rebuilt_today else [prev_upper] + older
    out = []
    for c in chain:
        if c not in out:
            out.append(c)
    return out[:MAX_BASES]


def build_one(feeds: dict, lower: dict, now: datetime, watch: set) -> dict:
    window, records, auto = {}, [], []
    for k, f in feeds.items():
        lo_s = lower.get(k) or SEED_LOWER[k]
        hi_s = f["generated_at"]
        lo, hi = pi._parse(lo_s), pi._parse(hi_s)
        window[k] = {"lower_exclusive": lo_s, "upper": hi_s,
                     "feed_regenerated_today": hi.astimezone(KST).date() == now.date()}
        if k == "x":
            r, a = x_records(f, lo, hi)
            records += r
            auto += a
        elif k == "yt":
            records += yt_records(f, lo, hi)
        else:
            records += web_records(f, lo, hi)
    watch_only = [r for r in records if r["person"] in watch]
    records = [r for r in records if r["person"] not in watch]
    order = {"core": 0, "shadow": 1, "observer": 2}
    records.sort(key=lambda r: (order.get(r["role"], 3), str(r.get("tier") or 9), r["person"] or "", r["feed"], r["at"]))
    vid_rid = {}
    for n, r in enumerate(records, 1):
        r["rid"] = f"R{n:03d}"
        if r.get("_vid"):
            vid_rid[r["_vid"]] = r["rid"]
    for r in records:
        same = [vid_rid[v] for v in r.pop("_same", []) if v in vid_rid]
        r.pop("_vid", None)
        if same:
            r["same_interview_as"] = same
    records = [{k: v for k, v in r.items() if v not in (None, "", [])} for r in records]
    by_person: dict[str, list[str]] = {}
    for r in records:
        by_person.setdefault(r["person"], []).append(r["rid"])
    return {
        "window": window,
        "expected_records": len(records),
        "rids": [r["rid"] for r in records],
        "by_person": by_person,
        "records": [{"rid": r["rid"], **{k: v for k, v in r.items() if k != "rid"}} for r in records],
        "watch_only_count": len(watch_only),
        "watch_only": [{k: w[k] for k in ("person", "at", "url", "preview") if w.get(k)} for w in watch_only],
        "auto_excluded_count": len(auto),
        "auto_excluded": [{k: a[k] for k in ("person", "at", "url", "preview") if a.get(k)} for a in auto],
    }


def build(now: datetime) -> tuple[dict, dict]:
    prev = load(OUT) if OUT.exists() else {}
    feeds = {k: load(p) for k, p in FEEDS.items()}
    watch = {p["person"] for p in load(PEOPLE).get("people", []) if p.get("watch_only")}
    lowers = base_lowers(prev, now)
    main = {"schema": "guru-manifest/v1", "built_at": kst(now), "note": FIELDS_NOTE,
            **build_one(feeds, lowers[0], now, watch), "base_lowers": lowers}
    sets = []
    for i, lo in enumerate(lowers[1:], 2):
        sets.append({"set": f"C{i}", **build_one(feeds, lo, now, watch)})
    catchup = {"schema": "guru-manifest-catchup/v1", "built_at": main["built_at"], "note": CATCHUP_NOTE, "sets": sets}
    return main, catchup


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    now = datetime.now(KST)
    if not args.force and now.hour != 10:
        print("not the morning slot; skipped")
        return 0
    m, c = build(now)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    for path, obj in ((CATCHUP, c), (OUT, m)):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    print(json.dumps({k: m[k] for k in ("built_at", "window", "expected_records", "auto_excluded_count")},
                     ensure_ascii=False, indent=2))
    for st in c["sets"]:
        print(st["set"], {k: v["lower_exclusive"] for k, v in st["window"].items()}, st["expected_records"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
