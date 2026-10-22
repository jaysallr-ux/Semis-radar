"""무료 X 수집 경로: 게시물 ID 탐색 + X 공개 엔드포인트로 원문 검증.

탐색 (어떤 글이 새로 올라왔나)
- X 공개 프로필 임베드 타임라인 (무료, 막히면 건너뜀)
- Grok(xAI) x_search (XAI_API_KEY 있을 때만). Grok 이 쓴 문장은 쓰지 않고 출처 URL 만 뽑는다

검증 (그 글이 진짜 그 사람 글인가, 원문은 뭔가)
- 1순위 X syndication tweet-result: 작성자, 정확한 시각, 원문, 답글/인용 여부
- 2순위 X oEmbed: 작성자, 원문 HTML (시각은 게시물 ID snowflake 로 계산)
작성자가 추적 대상 핸들과 다르면 버린다. 남에게 단 답글도 버린다.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

import math

XAI_URL = "https://api.x.ai/v1/responses"
SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"
TIMELINE_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
OEMBED_URL = "https://publish.twitter.com/oembed"
STATUS_RE = re.compile(r"(?:x|twitter)\.com/(?:(?P<handle>[A-Za-z0-9_]{1,15})|i(?:/web)?)/status/(?P<id>\d{5,25})")
TWITTER_EPOCH_MS = 1288834974657


def snowflake_time(post_id: str) -> str:
    ms = (int(post_id) >> 22) + TWITTER_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _to_base36(x: float) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    ip, fp = int(x), x - int(x)
    out = ""
    while ip:
        ip, r = divmod(ip, 36)
        out = digits[r] + out
    out = out or "0"
    if fp:
        out += "."
        for _ in range(12):
            fp *= 36
            d = int(fp)
            out += digits[d]
            fp -= d
            if not fp:
                break
    return out


def syndication_token(post_id: str) -> str:
    return re.sub(r"(0+|\.)", "", _to_base36((int(post_id) / 1e15) * math.pi))


def extract_status_ids(resp: dict) -> dict[str, str | None]:
    """{post_id: handle_or_None} from citations, annotations, and output text."""
    urls: list[str] = []
    for c in resp.get("citations") or []:
        urls.append(c if isinstance(c, str) else (c.get("url") or ""))
    for item in resp.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            for ann in part.get("annotations") or []:
                if isinstance(ann, dict) and ann.get("url"):
                    urls.append(ann["url"])
            if isinstance(part.get("text"), str):
                urls.append(part["text"])
    found: dict[str, str | None] = {}
    for u in urls:
        for m in STATUS_RE.finditer(u or ""):
            pid, h = m.group("id"), m.group("handle")
            if h and h.lower() in ("i", "intent", "home", "search"):
                h = None
            if pid not in found or (found[pid] is None and h):
                found[pid] = h
    return found


def oembed_text(html: str) -> str:
    m = re.search(r"<p[^>]*>(.*?)</p>", html or "", re.S)
    if not m:
        return ""
    body = m.group(1)
    body = re.sub(r"<br\s*/?>", "\n", body)
    body = re.sub(r"<a [^>]*>(.*?)</a>", r"\1", body, flags=re.S)
    body = re.sub(r"<[^>]+>", "", body)
    return htmllib.unescape(body).strip()


def handle_from_author_url(url: str) -> str | None:
    m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/?$", url or "")
    return m.group(1) if m else None


class GrokOembed:
    def __init__(self, api_key: str | None, http_post: Callable | None = None, http_get: Callable | None = None):
        self.api_key = api_key
        self._post = http_post or self._requests_post
        self._get = http_get or self._requests_get

    def _requests_post(self, url: str, payload: dict) -> tuple[int, dict]:
        r = requests.post(url, json=payload, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=180)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text[:300]}

    @staticmethod
    def _requests_get(url: str, params: dict) -> tuple[int, dict]:
        r = requests.get(url, params=params, headers=UA, timeout=20)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text}

    def syndication(self, post_id: str) -> dict | None:
        status, body = self._get(SYNDICATION_URL, {"id": post_id, "lang": "en", "token": syndication_token(post_id)})
        if status == 200 and isinstance(body, dict) and body.get("id_str") == post_id and body.get("user"):
            return body
        return None

    def timeline_ids(self, handle: str) -> tuple[list[str], str]:
        """Returns (ids authored by handle, status). status: ok / blocked_<code> / no_data."""
        status, body = self._get(TIMELINE_URL.format(handle=handle), {})
        raw = body.get("raw") if isinstance(body, dict) else None
        if status != 200 or not raw:
            return [], f"blocked_{status}"
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.S)
        if not m:
            return [], "no_data"
        try:
            d = json.loads(m.group(1))
            entries = d["props"]["pageProps"]["timeline"]["entries"]
        except Exception:
            return [], "no_data"
        ids = []
        for e in entries:
            t = (e.get("content") or {}).get("tweet") or {}
            if t.get("id_str") and ((t.get("user") or {}).get("screen_name") or "").lower() == handle.lower():
                ids.append(t["id_str"])
        return ids, "ok"

    def search(self, model: str, handles: list[str], from_date: str, to_date: str) -> dict:
        prompt = (
            "List every post published by the following X accounts between "
            f"{from_date} and {to_date} (UTC): " + ", ".join("@" + h for h in handles) + ". "
            "Include original posts, quote posts, and the author's own thread continuations. "
            "Exclude reposts and replies to other people. "
            "For each post output only its URL in the form https://x.com/<handle>/status/<id>, one per line. "
            "Do not summarize."
        )
        payload = {
            "model": model,
            "input": [{"role": "user", "content": prompt}],
            "tools": [{"type": "x_search", "allowed_x_handles": handles, "from_date": from_date, "to_date": to_date}],
        }
        status, body = self._post(XAI_URL, payload)
        if status != 200:
            msg = body.get("error") if isinstance(body, dict) else None
            if isinstance(msg, dict):
                msg = msg.get("message")
            raise RuntimeError(f"xAI {status} {msg or json.dumps(body, ensure_ascii=False)[:200]}")
        return body

    def oembed(self, post_id: str, handle: str | None) -> dict | None:
        candidates = [f"https://twitter.com/{handle}/status/{post_id}"] if handle else []
        candidates.append(f"https://twitter.com/i/status/{post_id}")
        for url in candidates:
            status, body = self._get(OEMBED_URL, {"url": url, "omit_script": "true", "dnt": "true"})
            if status == 200 and isinstance(body, dict) and body.get("html"):
                return body
        return None


def usage_cost(resp: dict, prices: dict) -> dict:
    u = resp.get("usage") or {}
    tin = u.get("input_tokens") or u.get("prompt_tokens") or 0
    tout = u.get("output_tokens") or u.get("completion_tokens") or 0
    tool = u.get("server_side_tool_usage_details") or {}
    posts = tool.get("x_posts_fetched") or 0
    users = tool.get("x_users_fetched") or 0
    usd = (tin * prices["input_per_m"] + tout * prices["output_per_m"]) / 1e6 + posts * prices["x_post"] + users * prices["x_user"]
    return {"input_tokens": tin, "output_tokens": tout, "x_posts_fetched": posts, "x_users_fetched": users,
            "usd_estimate": round(usd, 4)}


def verify(client: GrokOembed, post_id: str, hint: str | None, by_handle: dict) -> tuple[dict | None, str]:
    """Returns (record or None, outcome). outcome: syndication / oembed / rejected_author / reply_other / failed."""
    syn = client.syndication(post_id)
    if syn:
        h = (syn.get("user") or {}).get("screen_name") or ""
        acc = by_handle.get(h.lower())
        if not acc:
            return None, "rejected_author"
        reply_to = syn.get("in_reply_to_screen_name")
        if reply_to and reply_to.lower() != h.lower():
            return None, "reply_other"
        note = (((syn.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}).get("text")
        text = htmllib.unescape(note or syn.get("text") or "").strip()
        quoted = (syn.get("quoted_tweet") or {}).get("id_str")
        return {
            "id": post_id, "person": acc["person"], "handle": acc["handle"], "tier": acc["tier"],
            "author_id": (syn.get("user") or {}).get("id_str"),
            "created_at": syn.get("created_at") or snowflake_time(post_id),
            "url": f"https://x.com/{acc['handle']}/status/{post_id}",
            "text": text, "quoted_post_id": quoted, "thread": [],
            "is_self_reply": bool(reply_to),
            "verification": "syndication",
            "possibly_truncated": (not note) and (text.endswith("…") or text.endswith("...")),
        }, "syndication"
    emb = client.oembed(post_id, hint)
    if not emb:
        return None, "failed"
    h = handle_from_author_url(emb.get("author_url", ""))
    acc = by_handle.get((h or "").lower())
    if not acc:
        return None, "rejected_author"
    text = oembed_text(emb["html"])
    return {
        "id": post_id, "person": acc["person"], "handle": acc["handle"], "tier": acc["tier"],
        "author_id": None, "created_at": snowflake_time(post_id),
        "url": f"https://x.com/{acc['handle']}/status/{post_id}",
        "text": text, "quoted_post_id": None, "thread": [], "is_self_reply": None,
        "verification": "oembed",
        "possibly_truncated": text.endswith("…") or text.endswith("..."),
    }, "oembed"


def collect(client: GrokOembed, cfg: dict, accounts: list[dict], now: datetime, known_ids: set[str],
            manual_ids: dict[str, str | None] | None = None) -> dict:
    """Free discovery (profile embed) + optional Grok discovery, then free verification.
    Returns {"records", "cost", "stats", "discovered_by_handle"}. Grok failure is recorded, not raised."""
    g = cfg.get("grok", {})
    by_handle = {a["handle"].lower(): a for a in accounts}
    window = timedelta(days=cfg.get("window_days", 7))
    stats = {"timeline": {}, "grok": None, "manual": len(manual_ids or {}), "urls_found": 0, "already_known": 0, "too_old": 0,
             "verified_syndication": 0, "verified_oembed": 0, "rejected_author": 0, "reply_other": 0, "verify_failed": 0}
    found: dict[str, str | None] = {}
    sources: dict[str, set] = {}

    for a in accounts:
        try:
            ids, st = client.timeline_ids(a["handle"])
        except Exception as e:
            ids, st = [], f"error_{type(e).__name__}"
        stats["timeline"][a["handle"]] = st
        for pid in ids:
            found.setdefault(pid, a["handle"])
            sources.setdefault(pid, set()).add("profile_embed")

    for pid, h in (manual_ids or {}).items():
        if pid not in found or (found[pid] is None and h):
            found[pid] = h
        sources.setdefault(pid, set()).add("manual_browser")

    cost = None
    if client.api_key:
        lookback = timedelta(days=g.get("lookback_days", 2))
        try:
            resp = client.search(g.get("model", "grok-4.3"), [a["handle"] for a in accounts],
                                 (now - lookback).strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d"))
            cost = usage_cost(resp, g.get("prices", {"input_per_m": 1.25, "output_per_m": 2.5, "x_post": 0.005, "x_user": 0.01}))
            gids = extract_status_ids(resp)
            stats["grok"] = f"ok ({len(gids)} urls)"
            for pid, h in gids.items():
                if pid not in found or (found[pid] is None and h):
                    found[pid] = h
                sources.setdefault(pid, set()).add("grok")
        except RuntimeError as e:
            stats["grok"] = f"fail: {e}"
    else:
        stats["grok"] = "skipped (no XAI_API_KEY)"

    stats["urls_found"] = len(found)
    records = []
    for pid, hint in found.items():
        if pid in known_ids:
            stats["already_known"] += 1
            continue
        if now - datetime.fromisoformat(snowflake_time(pid).replace("Z", "+00:00")) > window:
            stats["too_old"] += 1
            continue
        rec, outcome = verify(client, pid, hint, by_handle)
        key = {"syndication": "verified_syndication", "oembed": "verified_oembed", "rejected_author": "rejected_author",
               "reply_other": "reply_other", "failed": "verify_failed"}[outcome]
        stats[key] += 1
        if rec:
            rec["discovered_via"] = sorted(sources.get(pid, []))
            records.append(rec)
    return {"records": records, "cost": cost, "stats": stats}


def oembed_selftest(client: GrokOembed, post_id: str, expect_handle: str) -> str:
    syn = client.syndication(post_id)
    syn_ok = bool(syn) and ((syn.get("user") or {}).get("screen_name") or "").lower() == expect_handle.lower()
    return ("syndication ok" if syn_ok else "syndication fail") + ", " + _oembed_check(client, post_id, expect_handle)


def _oembed_check(client: GrokOembed, post_id: str, expect_handle: str) -> str:
    emb = client.oembed(post_id, expect_handle)
    if not emb:
        return "oembed fail: 응답 없음"
    h = handle_from_author_url(emb.get("author_url", ""))
    if (h or "").lower() != expect_handle.lower():
        return f"oembed fail: author 불일치 ({h})"
    return "oembed ok" if oembed_text(emb["html"]) else "oembed fail: 본문 파싱 실패"
