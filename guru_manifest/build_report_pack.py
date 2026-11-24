"""Build the 11:00 report pack: the feed entries behind each manifest set, copied verbatim, so the report does not
have to read ~800 KB of raw feeds.

One JSON file per ledger set (main, C2, C3): guru_manifest/data/report_pack_<set>.json
- records[]: each manifest record with the FULL feed entry (or entries, for reply/repost bundles) exactly as it is
  in the feed: nothing is renamed, judged, shortened or dropped.
- feed_meta: each feed's generated_at, data_ok, discovery/processing flags, errors, per-account / channel / person
  health, and the YouTube grading thresholds (quote_verify_threshold, quote_min_chars_for_a).
- watch_only[]: watch_only posts with their full feed entries.
- outside_ledger[]: every feed entry whose clock falls in the set's window but is not a ledger record, with the code
  reason (auto_excluded, appearance not confirmed, not about the person, same appearance already covered, ...) and the
  full entry. The ledger fixes the reading section only (rules section 9); nothing in the window is hidden.
- body_sha256: sha256 of the canonical JSON of the whole object without this key
  (json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).

The build fails (exit 1, no pack written for that set) if any record has no matching feed entry.
Usage: python guru_manifest/build_report_pack.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "guru_manifest"))
import person_index as pi  # noqa: E402

DATA = REPO / "guru_manifest" / "data"
FEEDS = {
    "x": REPO / "guru_x" / "data" / ("feed_full.json" if (REPO / "guru_x" / "data" / "feed_full.json").exists() else "feed.json"),
    "yt": REPO / "guru_yt" / "data" / "youtube_feed.json",
    "web": REPO / "guru_web" / "data" / "web_feed.json",
}
META_KEYS = {
    "x": ("schema", "generated_at", "data_ok", "provider", "source", "discovery_complete", "window_days", "budget_stop", "errors", "accounts"),
    "yt": ("schema", "generated_at", "data_ok", "source", "new_statement_clock", "quote_verify_threshold", "quote_min_chars_for_a", "errors", "channels", "discovery"),
    "web": ("schema", "generated_at", "data_ok", "source", "new_statement_clock", "discovery_complete", "processing_complete", "errors", "people"),
}


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def canon(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def body_hash(pack: dict) -> str:
    return hashlib.sha256(canon({k: v for k, v in pack.items() if k != "body_sha256"})).hexdigest()


def indexes(feeds: dict) -> dict:
    return {"x": {p.get("url"): p for p in feeds["x"].get("posts", [])},
            "yt": {v.get("url"): v for v in feeds["yt"].get("videos", [])},
            "web": {i.get("canonical_url"): i for i in feeds["web"].get("items", [])}}


def in_window(feeds: dict, window: dict) -> dict:
    """Feed entries whose clock (rules section 8, same as build_manifest) is inside the window: url -> feed key."""
    out = {}
    lo, hi = {k: pi._parse(window[k]["lower_exclusive"]) for k in window}, {k: pi._parse(window[k]["upper"]) for k in window}
    for p in feeds["x"].get("posts", []):
        at, seen = pi._parse(p.get("created_at")), pi._parse(p.get("first_seen_at"))
        vt = p.get("video_transcript") or {}
        vt_at = pi._parse(vt.get("at")) if vt.get("status") == "ready" else None
        if at and ((lo["x"] < at <= hi["x"]) or (vt_at and at <= lo["x"] < vt_at <= hi["x"]) or (seen and at <= lo["x"] < seen <= hi["x"])):
            out[p.get("url")] = "x"
    for v in feeds["yt"].get("videos", []):
        at = pi._parse(v.get("evidence_ready_at") or v.get("first_seen_at"))
        if at and lo["yt"] < at <= hi["yt"]:
            out[v.get("url")] = "yt"
    for i in feeds["web"].get("items", []):
        at = pi._parse(i.get("evidence_ready_at") or i.get("discovered_at"))
        if at and lo["web"] < at <= hi["web"]:
            out[i.get("canonical_url")] = "web"
    return out


def reason(kind: str, e: dict, auto_urls: set) -> str:
    if kind == "x":
        if e.get("url") in auto_urls:
            return "auto_excluded (games / bare reactions, removed by code)"
        if (e.get("video_transcript") or {}).get("same_as_youtube"):
            return "X clip of a YouTube appearance already covered by an earlier report"
        return "not a ledger record (see build_manifest rules)"
    if kind == "yt":
        return "appearance_confirmed is not true" if not e.get("appearance_confirmed") else "not a ledger record"
    return "gemini_status=not_about_person" if e.get("gemini_status") == "not_about_person" else "not a ledger record"


def build_pack(set_id: str, s: dict, manifest_built_at: str, feeds: dict, idx: dict) -> tuple[dict, list[str]]:
    missing, records, used = [], [], set()
    feed_of = {"X": "x", "YouTube": "yt", "web": "web"}
    for r in s["records"]:
        k = feed_of[r["feed"]]
        urls = [it["url"] for it in r.get("items", [])] if r.get("kind") in ("reply_bundle", "repost_bundle") else [r.get("url")]
        entries = []
        for u in urls:
            e = idx[k].get(u)
            if e is None:
                missing.append(f"{r['rid']} {u}")
            else:
                entries.append(e)
                used.add(u)
        records.append({"rid": r["rid"], "manifest_record": r, "feed": k, "feed_entries": entries})
    watch = []
    for w in s.get("watch_only", []):
        k, e = next(((k, idx[k][w.get("url")]) for k in ("x", "yt", "web") if w.get("url") in idx[k]), (None, None))
        watch.append({"manifest_watch_only": w, "feed": k, "feed_entry": e})
        if e is None:
            missing.append(f"watch_only {w.get('url')}")
        used.add(w.get("url"))
    auto_urls = {a.get("url") for a in s.get("auto_excluded", [])}
    outside = []
    for u, k in sorted(in_window(feeds, s["window"]).items()):
        e = idx[k].get(u)
        if u not in used and e is not None:
            outside.append({"feed": k, "url": u, "reason": reason(k, e, auto_urls), "feed_entry": e})
    pack = {
        "schema": "guru-report-pack/v1",
        "set": set_id,
        "manifest_built_at": manifest_built_at,
        "expected_records": s["expected_records"],
        "rids": s["rids"],
        "window": s["window"],
        "feeds_generated_at": {k: feeds[k].get("generated_at") for k in ("x", "yt", "web")},
        "feed_meta": {k: {m: feeds[k].get(m) for m in META_KEYS[k] if m in feeds[k]} for k in ("x", "yt", "web")},
        "records": records,
        "watch_only": watch,
        "outside_ledger": outside,
    }
    pack["body_sha256"] = body_hash(pack)
    return pack, missing


def main() -> int:
    main_j = load(DATA / "manifest.json")
    catch = load(DATA / "manifest_catchup.json") if (DATA / "manifest_catchup.json").exists() else {"sets": []}
    feeds = {k: load(p) for k, p in FEEDS.items()}
    idx = indexes(feeds)
    for old in list(DATA.glob("report_pack_*.json")) + list(DATA.glob("report_pack_*.md")):
        old.unlink()
    rc = 0
    for sid, s in [("main", main_j)] + [(c["set"], c) for c in catch.get("sets", [])]:
        pack, missing = build_pack(sid, s, main_j["built_at"], feeds, idx)
        if missing:
            print(f"pack {sid} NOT written: feed entry missing for {missing}", file=sys.stderr)
            rc = 1
            continue
        out = DATA / f"report_pack_{sid}.json"
        out.write_text(canon(pack).decode("utf-8") + "\n", encoding="utf-8")
        print(sid, len(s["rids"]), "records ·", len(pack["outside_ledger"]), "outside ·", f"{out.stat().st_size // 1024} KB", pack["body_sha256"][:16])
    return rc


if __name__ == "__main__":
    sys.exit(main())
