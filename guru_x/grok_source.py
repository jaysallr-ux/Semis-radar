"""Grok(xAI) x_search 로 게시물 위치(URL)만 찾고, 원문과 작성자는 X 공개 oEmbed 로 확인.

Grok 이 쓴 문장은 쓰지 않는다. 응답의 citations / annotations / 본문에 나온 x.com status URL 만 뽑아서
publish.twitter.com/oembed 로 원문 HTML 과 author_url 을 받고, 추적 대상 핸들과 일치하는 것만 남긴다.
작성 시각은 게시물 ID(snowflake)에 들어 있는 타임스탬프로 계산한다.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

XAI_URL = "https://api.x.ai/v1/responses"
OEMBED_URL = "https://publish.twitter.com/oembed"
STATUS_RE = re.compile(r"(?:x|twitter)\.com/(?:(?P<handle>[A-Za-z0-9_]{1,15})|i(?:/web)?)/status/(?P<id>\d{5,25})")
TWITTER_EPOCH_MS = 1288834974657


def snowflake_time(post_id: str) -> str:
    ms = (int(post_id) >> 22) + TWITTER_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


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
    def __init__(self, api_key: str, http_post: Callable | None = None, http_get: Callable | None = None):
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
        r = requests.get(url, params=params, timeout=20)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text[:300]}

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


def collect(client: GrokOembed, cfg: dict, accounts: list[dict], now: datetime, known_ids: set[str]) -> dict:
    """Returns {"records": [...], "cost": {...}, "stats": {...}}. Raises RuntimeError on xAI failure."""
    g = cfg.get("grok", {})
    lookback = timedelta(days=g.get("lookback_days", 2))
    from_date = (now - lookback).strftime("%Y-%m-%d")
    to_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    by_handle = {a["handle"].lower(): a for a in accounts}

    resp = client.search(g.get("model", "grok-4.3"), [a["handle"] for a in accounts], from_date, to_date)
    cost = usage_cost(resp, g.get("prices", {"input_per_m": 1.25, "output_per_m": 2.5, "x_post": 0.005, "x_user": 0.01}))
    ids = extract_status_ids(resp)

    records, stats = [], {"urls_found": len(ids), "already_known": 0, "verified": 0, "rejected_author": 0, "oembed_failed": 0}
    for pid, hint in ids.items():
        if pid in known_ids:
            stats["already_known"] += 1
            continue
        emb = client.oembed(pid, hint)
        if not emb:
            stats["oembed_failed"] += 1
            continue
        h = handle_from_author_url(emb.get("author_url", ""))
        acc = by_handle.get((h or "").lower())
        if not acc:
            stats["rejected_author"] += 1
            continue
        text = oembed_text(emb["html"])
        records.append({
            "id": pid,
            "person": acc["person"],
            "handle": acc["handle"],
            "tier": acc["tier"],
            "author_id": None,
            "created_at": snowflake_time(pid),
            "url": f"https://x.com/{acc['handle']}/status/{pid}",
            "text": text,
            "quoted_post_id": None,
            "thread": [],
            "verification": "oembed",
            "possibly_truncated": text.endswith("…") or text.endswith("..."),
        })
        stats["verified"] += 1
    return {"records": records, "cost": cost, "stats": stats}


def oembed_selftest(client: GrokOembed, post_id: str, expect_handle: str) -> str:
    emb = client.oembed(post_id, expect_handle)
    if not emb:
        return "fail: oEmbed 응답 없음"
    h = handle_from_author_url(emb.get("author_url", ""))
    if (h or "").lower() != expect_handle.lower():
        return f"fail: author 불일치 ({h})"
    return "ok" if oembed_text(emb["html"]) else "fail: 본문 파싱 실패"
