import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_flow as cf  # noqa: E402

CFG = json.loads((Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8"))
P = CFG["people"][0]
NOW = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)
FILLER = ("<p>Stocks extended their slide on Tuesday as investors weighed hawkish comments from Federal Reserve "
          "officials and fresh worries about artificial intelligence spending across the technology sector.</p>"
          "<p>Treasury yields rose across the curve while the dollar firmed against most major currencies, adding "
          "pressure on equities that had already lost ground earlier in the month.</p>")


def page(paras: str, extras: str = "") -> str:
    return (f"<html><body><nav><p>Markets home McElligott latest headlines and more news</p></nav>"
            f"<article>{FILLER}{paras}"
            f"<div class='related-stories'><p>Related: Nomura's McElligott said last month that everything is fine forever.</p></div>"
            f"<aside><p>McElligott warned in an old note that you should subscribe to our newsletter today.</p></aside>"
            f"</article>{extras}<footer><p>Copyright notice and other footer boilerplate text here.</p></footer></body></html>")


COND = page("<p>Nomura's Charlie McElligott said CTAs could sell up to $50 billion of US equities over the next week "
            "if the S&amp;P 500 breaks below 6,400.</p><p>Below that level the selling would accelerate.</p>"
            "<p>Dealer gamma is short, McElligott wrote in a note, which amplifies moves.</p>")
MENTION = page("<p>Strategists including Nomura's Charlie McElligott have been watching systematic positioning closely "
               "this month as volatility picked up.</p>")


def rss(items):
    body = "".join(
        f"<item><title>{t} - {o}</title><link>https://news.google.com/rss/articles/{aid}</link>"
        f"<pubDate>{d}</pubDate><source>{o}</source></item>" for t, o, aid, d in items)
    return f"<rss><channel>{body}</channel></rss>"


def make_http(items, pages):
    """pages: aid -> (publisher url, html or None for paywall). Mutable so a test can 'unlock' a page later."""
    def get(url, params=None, headers=None):
        if url.startswith("https://news.google.com/rss/search"):
            return 200, rss(items), url
        if url.startswith("https://news.google.com/articles/"):
            aid = url.rsplit("/", 1)[1]
            return 200, f'<div data-n-a-sg="SG{aid}" data-n-a-ts="1"></div>', url
        for _aid, (purl, html) in pages.items():
            if url == purl:
                return (200, html, url) if html is not None else (200, "<p>Subscribe to read.</p>", url)
        return 404, "", url

    def post(url, data=None, headers=None):
        for aid, (purl, _html) in pages.items():
            if f"SG{aid}" in data:
                return 200, f'[\\"garturlres\\",\\"{purl}\\"]'
        return 200, ""
    return cf.Http(get=get, post=post)


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "NOTES_PATH", tmp_path / "notes.json")
    monkeypatch.setattr(cf, "DIGEST_PATH", tmp_path / "digest.json")
    monkeypatch.setattr(cf, "RUNS_PATH", tmp_path / "runs.csv")


def test_article_text_skips_nav_related_aside_footer():
    t = cf.article_text(COND)
    assert "everything is fine forever" not in t and "newsletter" not in t
    assert "Markets home" not in t and "Copyright" not in t
    assert "$50 billion" in t and "S&P 500" in t


def test_attribution_kinds():
    ex = cf.classify(P, cf.article_text(COND), 320)
    kinds = [(e["kind"], e["text"][:30]) for e in ex]
    assert kinds[0][0] == "attributed" and "$50 billion" in ex[0]["text"]
    assert ex[1] == {"kind": "context", "text": "Below that level the selling would accelerate."}
    assert ex[2]["kind"] == "attributed" and "wrote in a note" in ex[2]["text"]
    assert [e["kind"] for e in cf.classify(P, cf.article_text(MENTION), 320)] == ["mention"]
    assert cf.attributed(P, "The selling will accelerate, according to Nomura's Charlie McElligott.")
    assert cf.attributed(P, "“CTAs are max short,” said Nomura's McElligott.")
    assert not cf.attributed(P, "Investors are watching McElligott's CTA estimates closely.")
    assert not cf.attributed(P, "McElligott's CTA model shows selling.")
    assert not cf.attributed(P, "Analysts who follow McElligott say flows matter.")
    assert not cf.attributed(P, "Strategists like Nomura's Charlie McElligott say flows matter.")
    assert cf.attributed(P, "McElligott, a strategist at Nomura, said CTAs are short.")
    assert cf.attributed(P, "In a note Tuesday, McElligott flagged a gamma flip level at 6,500.")


def test_conditional_note_is_judgable_with_levels(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    items = [("Nomura's McElligott warns CTAs could sell $50 billion", "ZeroHedge", "a1", "Tue, 25 Nov 2026 14:00:00 GMT")]
    out = cf.run(make_http(items, {"a1": ("https://www.zerohedge.com/markets/cta?utm_source=x", COND)}), NOW, CFG)
    n = out["notes"][0]
    assert n["judgable"] and n["own_statement"]
    assert {"cta", "dealer_gamma"} <= set(n["topics"])
    assert {"if", "below", "could"} <= set(n["conditional_cues"])
    assert "$50 billion" in n["levels"] and "6,400" in n["levels"]
    assert "발동 가격" in n["judging_note"]
    assert n["sources"][0]["url"] == "https://www.zerohedge.com/markets/cta"


def test_mention_only_is_not_judgable(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    items = [("Strategists like Nomura's McElligott watch positioning", "Reuters", "m1", "Tue, 25 Nov 2026 14:00:00 GMT")]
    out = cf.run(make_http(items, {"m1": ("https://reuters.com/m1", MENTION)}), NOW, CFG)
    n = out["notes"][0]
    assert n["body_read"] and not n["own_statement"] and not n["judgable"]
    assert n["sources"][0]["body_status"] == "mention_only"
    assert "본인 발언 미확인" in n["judging_note"]


def test_paywalled_source_is_retried_and_recovered(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    items = [("Nomura's McElligott: CTAs to dump $50 billion", "Bloomberg", "b1", "Tue, 25 Nov 2026 14:00:00 GMT")]
    pages = {"b1": ("https://www.bloomberg.com/news/x", None)}
    out = cf.run(make_http(items, pages), NOW, CFG)
    s = out["notes"][0]["sources"][0]
    assert s["body_status"] == "paywalled_or_empty" and not out["notes"][0]["judgable"]
    # too soon: no retry
    out = cf.run(make_http(items, pages), NOW + timedelta(hours=2), CFG)
    assert out["run_stats"]["retried"] == 0
    # later and the page is readable now (syndicated copy, paywall lifted...)
    pages["b1"] = ("https://www.bloomberg.com/news/x", COND)
    out = cf.run(make_http(items, pages), NOW + timedelta(hours=12), CFG)
    assert out["run_stats"]["retried"] == 1 and out["run_stats"]["retry_recovered"] == 1
    assert out["notes"][0]["judgable"] and out["notes"][0]["sources"][0]["attempts"] == 2


def test_retry_stops_after_max_attempts(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    items = [("Nomura's McElligott: CTAs to dump $50 billion", "Bloomberg", "b1", "Tue, 25 Nov 2026 14:00:00 GMT")]
    pages = {"b1": ("https://www.bloomberg.com/news/x", None)}
    for h in (0, 12, 24, 36, 48):
        out = cf.run(make_http(items, pages), NOW + timedelta(hours=h), CFG)
    assert out["notes"][0]["sources"][0]["attempts"] == CFG["max_attempts"]


def test_dedupe_same_quote_merges_but_shared_number_does_not(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    other = page("<p>Separately, McElligott wrote that vol-control funds could sell $50 billion if realized volatility "
                 "stays elevated into opex.</p>")
    items = [
        ("Nomura's McElligott warns CTAs could sell $50 billion", "ZeroHedge", "a1", "Tue, 25 Nov 2026 14:00:00 GMT"),
        ("Stocks face more pressure from trend followers, Nomura's McElligott says", "Business Insider", "a2", "Tue, 25 Nov 2026 18:00:00 GMT"),
        ("Nomura's McElligott: CTA, vol funds and $50 billion of supply", "MarketWatch", "a3", "Tue, 25 Nov 2026 20:00:00 GMT"),
    ]
    pages = {"a1": ("https://zh.com/1", COND), "a2": ("https://bi.com/2", COND), "a3": ("https://mw.com/3", other)}
    out = cf.run(make_http(items, pages), NOW, CFG)
    assert len(out["notes"]) == 2
    cta = next(n for n in out["notes"] if any(s["url"] == "https://zh.com/1" for s in n["sources"]))
    assert {s["url"] for s in cta["sources"]} == {"https://zh.com/1", "https://bi.com/2"}
    out2 = cf.run(make_http(items, pages), NOW + timedelta(hours=1), CFG)
    assert len(out2["notes"]) == 2 and out2["run_stats"]["new_sources"] == 0


def test_digest_caps_output_not_storage_and_metrics(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    items = [(f"Nomura's McElligott note number {i} on topic {chr(97 + i)}{chr(98 + i)}", "Outlet", f"c{i}",
              f"Tue, 25 Nov 2026 {10 + i:02d}:00:00 GMT") for i in range(8)]
    pages = {f"c{i}": (f"https://o.com/{i}", COND if i == 0 else None) for i in range(8)}
    cfg = dict(CFG, same_note_title_sim=0.99, same_quote_sim=1.01)
    out = cf.run(make_http(items, pages), NOW, cfg)
    digest = json.loads((tmp_path / "digest.json").read_text(encoding="utf-8"))
    assert len(out["notes"]) == 8
    assert len(digest["notes"]) == CFG["digest_cap"] and digest["dropped_by_cap"] == 3
    assert digest["notes"][0]["judgable"]          # judgable notes first
    m = out["metrics"]
    assert m["sources_tried"] == 8 and m["body_read"] == 1 and m["body_read_rate_pct"] == 12.5
    assert m["own_statement_notes"] == 1 and m["own_statement_with_levels"] == 1
    assert "own_statement_rate_pct" in (tmp_path / "runs.csv").read_text().splitlines()[0]


def test_unrelated_title_skipped(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    items = [("Nomura raises Japan GDP forecast", "Reuters", "d1", "Tue, 25 Nov 2026 14:00:00 GMT")]
    out = cf.run(make_http(items, {}), NOW, CFG)
    assert out["notes"] == []


# ---------- regressions from the GPT review of v2 ----------

def test_opposite_direction_never_merges(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    assert cf.title_sim("McElligott says CTAs will buy $50 billion", "McElligott says CTAs will sell $50 billion") > 0.9
    assert cf.conflict("McElligott says CTAs will buy $50 billion", "McElligott says CTAs will sell $50 billion")
    assert cf.conflict("Stocks to rally, McElligott says", "Stocks to fall, McElligott says")
    assert cf.conflict("CTAs to sell $50 billion", "CTAs to sell $30 billion") == "numbers"
    assert cf.conflict("CTAs could sell $50 billion if SPX breaks 6400", "CTAs will sell $50 billion", True) == "condition"
    assert cf.conflict("CTAs sell below 6,400", "CTAs sell under SPX 6400") is None      # same level, two spellings
    assert cf.conflict("$50 billion of supply", "$50bn supply") is None
    items = [("Nomura's McElligott says CTAs will buy $50 billion", "ZeroHedge", "x1", "Tue, 25 Nov 2026 14:00:00 GMT"),
             ("Nomura's McElligott says CTAs will sell $50 billion", "Business Insider", "x2", "Tue, 25 Nov 2026 15:00:00 GMT")]
    out = cf.run(make_http(items, {"x1": ("https://zh.com/x1", None), "x2": ("https://bi.com/x2", None)}), NOW, CFG)
    assert len(out["notes"]) == 2


def test_opposite_quotes_never_merge_even_if_similar(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    buy = page("<p>Nomura's Charlie McElligott said CTAs will buy $50 billion of US equities over the next week.</p>")
    sell = page("<p>Nomura's Charlie McElligott said CTAs will sell $50 billion of US equities over the next week.</p>")
    items = [("Trend followers set to move markets, Nomura's McElligott says", "A", "q1", "Tue, 25 Nov 2026 14:00:00 GMT"),
             ("Trend followers set to move markets again, Nomura's McElligott says", "B", "q2", "Tue, 25 Nov 2026 15:00:00 GMT")]
    out = cf.run(make_http(items, {"q1": ("https://a.com/1", buy), "q2": ("https://b.com/2", sell)}), NOW, CFG)
    assert len(out["notes"]) == 2


def test_third_party_reports_are_not_his_statement():
    for s in ("The reporter said McElligott was unavailable for comment.",
              "Analysts said Charlie McElligott was wrong about gamma.",
              "Analysts said McElligott warned too early.",
              "The firm said McElligott would not comment.",
              "According to McElligott's critics, the model is flawed."):
        assert not cf.attributed(P, s), s
    for s in ("“CTAs are max short,” said Nomura's McElligott.",
              "Selling could accelerate, said McElligott, a strategist at Nomura.",
              "McElligott expects CTAs to sell."):
        assert cf.attributed(P, s), s
    kinds = [e["kind"] for e in cf.classify(P, "The reporter said McElligott was unavailable for comment. "
                                               "Stocks fell sharply on the day.", 320)]
    assert kinds == ["mention"]


def test_commaless_index_levels_but_not_years():
    hits = {"SPX 6400": "6400", "S&P 500 breaks below 6400": "6400", "the 6400 strike": "6400",
            "selling below 6400.": "6400", "ES futures at 6385.5": "6385.5", "Russell 2000 at 2050": "2050",
            "a gamma flip at 6450 level": "6450"}
    for s, want in hits.items():
        assert want in cf.levels_of(s), s
    for s in ("outlook for 2026", "in 2026", "S&P 500 2026 forecast", "back to 2022 levels", "since 2019,",
              "below 2026.", "S&P 500 in 2026", "the Russell 2000 index"):
        assert cf.levels_of(s) == [], s
