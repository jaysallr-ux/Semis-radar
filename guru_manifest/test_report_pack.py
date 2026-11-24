"""Tests for build_report_pack.py. Run: python -m pytest guru_manifest/test_report_pack.py -q"""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_report_pack as bp  # noqa: E402


@pytest.fixture(scope="module")
def ctx():
    main = bp.load(bp.DATA / "manifest.json")
    catch = bp.load(bp.DATA / "manifest_catchup.json")
    feeds = {k: bp.load(p) for k, p in bp.FEEDS.items()}
    idx = bp.indexes(feeds)
    sets = [("main", main)] + [(c["set"], c) for c in catch["sets"]]
    packs = {sid: bp.build_pack(sid, s, main["built_at"], feeds, idx) for sid, s in sets}
    return main, sets, feeds, idx, packs


def test_no_missing_entries(ctx):
    *_, packs = ctx
    for sid, (_, missing) in packs.items():
        assert missing == [], sid


def test_rids_order_count_unique(ctx):
    _, sets, *_, packs = ctx
    for sid, s in sets:
        p = packs[sid][0]
        got = [r["rid"] for r in p["records"]]
        assert got == s["rids"] == p["rids"] and len(set(got)) == len(got) == s["expected_records"] == p["expected_records"]


def test_feed_entries_are_verbatim(ctx):
    _, sets, _, idx, packs = ctx
    key = {"X": "x", "YouTube": "yt", "web": "web"}
    for sid, s in sets:
        for rec, r in zip(packs[sid][0]["records"], s["records"]):
            urls = [it["url"] for it in r["items"]] if r.get("kind") in ("reply_bundle", "repost_bundle") else [r["url"]]
            assert rec["feed_entries"] == [idx[key[r["feed"]]][u] for u in urls], (sid, r["rid"])
            assert rec["manifest_record"] == r


def test_rule_fields_preserved(ctx):
    *_, packs = ctx
    p = packs["main"][0]
    x = [e for r in p["records"] if r["feed"] == "x" for e in r["feed_entries"]]
    assert x and all("verification" in e and "created_at" in e for e in x)
    rc = [c for e in x for c in e.get("reply_context") or []]
    assert rc and all("created_at" in c and "author" in c for c in rc)
    qp = [e["quoted_post"] for e in x if e.get("quoted_post")]
    assert qp and all("created_at" in q for q in qp)
    yt = [e for r in p["records"] if r["feed"] == "yt" for e in r["feed_entries"]]
    assert yt and all(e.get("appearance_confirmed") is True and "published_at" in e and "evidence_grade" in e for e in yt)
    assert any("caption_match" in q for e in yt for q in e.get("quotes") or [])
    assert p["feed_meta"]["yt"]["quote_verify_threshold"] and p["feed_meta"]["yt"]["quote_min_chars_for_a"]
    web = [e for r in p["records"] if r["feed"] == "web" for e in r["feed_entries"]]
    assert any(q.get("quote_match") is not None and "speaker_verified" in q for e in web for q in e.get("direct_quotes") or [])
    assert "accounts" in p["feed_meta"]["x"] and "channels" in p["feed_meta"]["yt"] and "people" in p["feed_meta"]["web"]


def test_everything_in_window_is_accounted(ctx):
    _, sets, feeds, _, packs = ctx
    for sid, s in sets:
        p = packs[sid][0]
        shown = {e.get("url") or e.get("canonical_url") for r in p["records"] for e in r["feed_entries"]}
        shown |= {w["manifest_watch_only"]["url"] for w in p["watch_only"]}
        shown |= {o["url"] for o in p["outside_ledger"]}
        assert set(bp.in_window(feeds, s["window"])) <= shown, sid


def test_generated_at_matches_main_window(ctx):
    main, *_, packs = ctx
    p = packs["main"][0]
    assert p["manifest_built_at"] == main["built_at"]
    assert all(p["feeds_generated_at"][k] == main["window"][k]["upper"] for k in ("x", "yt", "web"))


def test_hash_detects_any_change(ctx):
    *_, packs = ctx
    p = packs["main"][0]
    assert bp.body_hash(p) == p["body_sha256"]
    t = copy.deepcopy(p)
    t["records"][3]["feed_entries"][0]["text" if t["records"][3]["feed"] == "x" else "url"] += " "
    assert bp.body_hash(t) != p["body_sha256"]
    t = copy.deepcopy(p); t["records"].pop(10)
    assert bp.body_hash(t) != p["body_sha256"]


def test_hash_survives_file_roundtrip(ctx, tmp_path):
    *_, packs = ctx
    p = packs["main"][0]
    f = tmp_path / "p.json"
    f.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    q = json.loads(f.read_text(encoding="utf-8"))
    assert bp.body_hash(q) == q["body_sha256"] == p["body_sha256"]


def test_missing_feed_entry_is_reported(ctx):
    main, _, feeds, idx, _ = ctx
    idx2 = {k: dict(v) for k, v in idx.items()}
    r = next(r for r in main["records"] if r["feed"] == "YouTube")
    del idx2["yt"][r["url"]]
    _, missing = bp.build_pack("main", main, main["built_at"], feeds, idx2)
    assert any(m.startswith(r["rid"]) for m in missing)


def test_main_size(ctx):
    *_, packs = ctx
    size = len(bp.canon(packs["main"][0]))
    feeds = sum(p.stat().st_size for p in bp.FEEDS.values())
    assert size * 4 < feeds
