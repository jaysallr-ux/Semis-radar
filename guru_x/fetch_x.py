"""guru_x: 추적 대상 X 계정의 원문 게시물을 X 공식 API로 수집해 guru_x/data/feed.json 에 기록.

역할 분담 (semis-radar 와 동일)
- X API = 사실 (원문, 작성자 user_id, 시각, URL)
- Python = 수집/중복제거/보관 기간 관리
- GPT = feed.json 을 읽고 판정 (요약/해석은 GPT 몫, 여기서는 하지 않음)

비용 (2026-09 기준 pay-per-use): 게시물 1개 반환당 $0.005, 사용자 조회 1명당 $0.01.
같은 리소스는 UTC 하루 1회만 과금. since_id 로 이미 받은 글은 다시 받지 않음.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import requests

try:
    from guru_x import grok_source, media_vision
except ImportError:  # 직접 실행 시
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from guru_x import grok_source, media_vision

BASE = "https://api.x.com/2"
KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "accounts.json"
DATA_DIR = ROOT / "data"
STATE_PATH = DATA_DIR / "state.json"
FEED_PATH = DATA_DIR / "feed.json"
RUNS_PATH = DATA_DIR / "runs.csv"
MANUAL_PATH = DATA_DIR / "manual_ids.json"

PRICE_PER_POST = 0.005
PRICE_PER_USER = 0.010
TWEET_FIELDS = "created_at,note_tweet,referenced_tweets,conversation_id,author_id"


class BudgetExceeded(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status} {message}")
        self.status = status


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_kst(dt: datetime) -> str:
    return dt.astimezone(KST).isoformat(timespec="seconds")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class XClient:
    def __init__(self, token: str, budget_posts: int, http_get: Callable | None = None):
        self.token = token
        self.budget_posts = budget_posts
        self.posts_billed = 0
        self.users_billed = 0
        self._get = http_get or self._requests_get

    def _requests_get(self, url: str, params: dict) -> tuple[int, dict]:
        r = requests.get(url, params=params, headers={"Authorization": f"Bearer {self.token}"}, timeout=30)
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:300]}
        return r.status_code, body

    def get(self, path: str, params: dict) -> dict:
        url = f"{BASE}{path}"
        for attempt in range(2):
            status, body = self._get(url, params)
            if status == 200:
                return body
            if status >= 500 and attempt == 0:
                time.sleep(3)
                continue
            msg = ""
            if isinstance(body, dict):
                msg = body.get("detail") or body.get("title") or json.dumps(body, ensure_ascii=False)[:200]
            raise ApiError(status, msg)
        raise ApiError(599, "retry exhausted")

    def charge_posts(self, n: int) -> None:
        self.posts_billed += n

    def remaining(self) -> int:
        return self.budget_posts - self.posts_billed

    def lookup_users(self, handles: list[str]) -> dict[str, str]:
        body = self.get("/users/by", {"usernames": ",".join(handles), "user.fields": "username"})
        out = {}
        for u in body.get("data", []) or []:
            self.users_billed += 1
            out[u["username"].lower()] = u["id"]
        return out

    def user_posts(self, user_id: str, since_id: str | None, start_time: str | None, cap: int) -> list[dict]:
        posts: list[dict] = []
        token = None
        while len(posts) < cap:
            if self.remaining() <= 0:
                raise BudgetExceeded()
            page = max(5, min(100, cap - len(posts), self.remaining()))
            params = {"max_results": page, "exclude": "retweets,replies", "tweet.fields": TWEET_FIELDS}
            if since_id:
                params["since_id"] = since_id
            elif start_time:
                params["start_time"] = start_time
            if token:
                params["pagination_token"] = token
            body = self.get(f"/users/{user_id}/tweets", params)
            data = body.get("data", []) or []
            self.charge_posts(len(data))
            posts.extend(data)
            token = (body.get("meta") or {}).get("next_token")
            if not token or not data:
                break
        return posts[:cap]

    def self_thread(self, conversation_id: str, handle: str, user_id: str, cap: int) -> list[dict]:
        if self.remaining() <= 0:
            raise BudgetExceeded()
        params = {
            "query": f"conversation_id:{conversation_id} from:{handle} is:reply",
            "max_results": max(10, min(100, cap)),
            "tweet.fields": TWEET_FIELDS,
        }
        body = self.get("/tweets/search/recent", params)
        data = body.get("data", []) or []
        self.charge_posts(len(data))
        return [t for t in data if t.get("author_id") == user_id][:cap]


def post_text(t: dict) -> str:
    note = t.get("note_tweet") or {}
    return note.get("text") or t.get("text", "")


def quoted_id(t: dict) -> str | None:
    for ref in t.get("referenced_tweets") or []:
        if ref.get("type") == "quoted":
            return ref.get("id")
    return None


def to_record(t: dict, acc: dict) -> dict:
    return {
        "id": t["id"],
        "person": acc["person"],
        "handle": acc["handle"],
        "tier": acc["tier"],
        "role": acc.get("role", "core"),
        "author_id": t.get("author_id"),
        "created_at": t.get("created_at"),
        "url": f"https://x.com/{acc['handle']}/status/{t['id']}",
        "text": post_text(t),
        "quoted_post_id": quoted_id(t),
        "thread": [],
        "verification": "x_api",
        "possibly_truncated": False,
    }


def merge_thread(rec: dict, replies: list[dict], acc: dict) -> None:
    by_id = {r["id"]: r for r in rec.get("thread", [])}
    for t in replies:
        by_id[t["id"]] = {
            "id": t["id"],
            "created_at": t.get("created_at"),
            "url": f"https://x.com/{acc['handle']}/status/{t['id']}",
            "text": post_text(t),
        }
    rec["thread"] = sorted(by_id.values(), key=lambda r: r.get("created_at") or "")


def run(token: str | None, http_get: Callable | None = None, now: datetime | None = None,
        xai_key: str | None = None, grok_client=None) -> dict:
    now = now or now_utc()
    cfg = load_json(CONFIG_PATH, {})
    accounts = cfg["accounts"]
    window = timedelta(days=cfg.get("window_days", 7))
    recheck = timedelta(hours=cfg.get("thread_recheck_hours", 12))
    state = load_json(STATE_PATH, {"user_ids": {}, "since_id": {}})
    old_feed = load_json(FEED_PATH, {})
    posts = {p["id"]: p for p in old_feed.get("posts", [])}

    errors: dict[str, str] = {}
    acc_status: dict[str, dict] = {a["handle"]: {"last_fetch_ok": False, "new_originals": 0} for a in accounts}
    prev_status = {a.get("handle"): a for a in old_feed.get("accounts", [])}
    budget_stop = False

    provider = "x_api" if token else "free"
    client = XClient(token, cfg.get("max_posts_billed_per_run", 300), http_get) if provider == "x_api" else None

    if client:
        missing = [a["handle"] for a in accounts if a["handle"].lower() not in state["user_ids"]]
        if missing:
            try:
                found = client.lookup_users(missing)
                for h in missing:
                    if h.lower() in found:
                        state["user_ids"][h.lower()] = found[h.lower()]
                    else:
                        errors[h] = "계정 조회 실패 (핸들 확인 필요)"
            except ApiError as e:
                errors["_global"] = f"사용자 조회 실패: {e}"

        if "_global" not in errors:
            start_time = (now - window).strftime("%Y-%m-%dT%H:%M:%SZ")
            for acc in accounts:
                h = acc["handle"]
                uid = state["user_ids"].get(h.lower())
                if not uid or h in errors:
                    continue
                try:
                    new = client.user_posts(uid, state["since_id"].get(h.lower()), start_time,
                                            cfg.get("max_originals_per_account_per_run", 20))
                    for t in new:
                        if t.get("author_id") and t["author_id"] != uid:
                            continue
                        posts[t["id"]] = {**to_record(t, acc), "thread": posts.get(t["id"], {}).get("thread", [])}
                    if new:
                        state["since_id"][h.lower()] = str(max(int(t["id"]) for t in new))
                    acc_status[h]["new_originals"] = len(new)

                    recent = [p for p in posts.values()
                              if p["handle"] == h and p.get("created_at") and now - parse_ts(p["created_at"]) <= recheck]
                    for p in recent:
                        replies = client.self_thread(p["id"], h, uid, cfg.get("max_thread_replies_per_post", 15))
                        merge_thread(p, replies, acc)
                    acc_status[h]["last_fetch_ok"] = True
                except BudgetExceeded:
                    budget_stop = True
                    errors[h] = "이번 실행 과금 한도 도달로 중단"
                    break
                except ApiError as e:
                    errors[h] = str(e)
                    if e.status in (401, 403):
                        errors["_global"] = f"인증 실패: {e}"
                        break

    grok_cost, grok_stats, selftest = None, None, None
    gclient = grok_client or (grok_source.GrokOembed(xai_key) if provider == "free" else None)
    if provider == "free":
        manual = {m["id"]: m.get("handle_hint") for m in load_json(MANUAL_PATH, {}).get("ids", [])
                  if str(m.get("id", "")).isdigit()}
        # rev 4 added image URLs: re-verify (once) only the older media posts that lack them
        current = {pid for pid, r in posts.items()
                   if r.get("verification") == "x_api" or r.get("rev", 1) >= 4
                   or (r.get("rev", 1) >= 3 and not r.get("has_media") and not (r.get("quoted_post") or {}).get("has_media"))}
        refresh = {pid: r.get("handle") for pid, r in posts.items() if pid not in current}
        res = grok_source.collect(gclient, cfg, accounts, now, current, manual, refresh)
        for r in res["records"]:
            old_rec = posts.get(r["id"])
            if old_rec:
                r["discovered_via"] = sorted((set(old_rec.get("discovered_via", [])) | set(r["discovered_via"])) - {"refresh"})
            else:
                acc_status[r["handle"]]["new_originals"] += 1
            posts[r["id"]] = r
        grok_cost, grok_stats = res["cost"], res["stats"]
        grok_ok = (grok_stats.get("grok") or "").startswith("ok")
        manual_at = load_json(MANUAL_PATH, {}).get("updated_at")
        manual_fresh = bool(manual_at) and now - parse_ts(manual_at) <= timedelta(hours=14)
        list_ok = (grok_stats.get("list") or "").startswith("ok")
        for a in accounts:
            tl = grok_stats["timeline"].get(a["handle"], "")
            if list_ok or tl == "ok" or grok_ok or manual_fresh:
                acc_status[a["handle"]]["last_fetch_ok"] = True
            else:
                errors[a["handle"]] = (f"탐색 실패 (리스트 임베드: {grok_stats.get('list') or '미설정'}, "
                                       f"Grok: {grok_stats.get('grok')}, 수동 ID: {'최신' if manual_fresh else '없음/만료'})")
    if gclient is not None and cfg.get("oembed_selftest"):
        st_cfg = cfg["oembed_selftest"]
        try:
            selftest = grok_source.oembed_selftest(gclient, st_cfg["post_id"], st_cfg["handle"])
        except Exception as e:
            selftest = f"fail: {e}"

    for a in accounts:
        h = a["handle"]
        if not acc_status[h]["last_fetch_ok"] and h not in errors and "_global" not in errors:
            errors[h] = "이번 실행 미수집 (과금 한도 도달)" if budget_stop else "이번 실행 미수집"

    cutoff = now - window
    kept = [p for p in posts.values() if p.get("created_at") and parse_ts(p["created_at"]) >= cutoff]
    # carry previous image descriptions forward, then describe new images (context only, never evidence)
    old_ms = {p["id"]: p.get("media_summary") for p in old_feed.get("posts", []) if p.get("media_summary")}
    for p in kept:
        if not p.get("media_summary") and p["id"] in old_ms:
            p["media_summary"] = old_ms[p["id"]]
    vision_stats = media_vision.describe_posts(kept, state, now, os.environ.get("GEMINI_API_KEY") or None,
                                               cfg.get("media_vision", {}))
    kept.sort(key=lambda p: p["created_at"], reverse=True)

    discovery_complete = provider == "x_api"
    if provider == "free" and grok_stats and (grok_stats.get("list") or "").startswith("ok") and grok_stats.get("list_oldest"):
        oldest = parse_ts(grok_stats["list_oldest"])
        prev_ok = (old_feed.get("discovery_stats") or {}).get("list", "") or ""
        if prev_ok.startswith("ok") and old_feed.get("generated_at"):
            discovery_complete = oldest <= parse_ts(old_feed["generated_at"])
        else:
            discovery_complete = oldest <= now - timedelta(hours=24)

    fetched_at = iso_kst(now)
    acc_out = []
    for a in accounts:
        h = a["handle"]
        st = acc_status[h]
        prev = prev_status.get(h, {})
        acc_out.append({
            "person": a["person"],
            "handle": h,
            "tier": a["tier"],
            "role": a.get("role", "core"),
            "user_id": state["user_ids"].get(h.lower()),
            "last_fetch_ok": st["last_fetch_ok"],
            "last_success_at": fetched_at if st["last_fetch_ok"] else prev.get("last_success_at"),
            "new_originals_this_run": st["new_originals"],
            "error": errors.get(h) or errors.get("_global"),
        })

    posts_billed = client.posts_billed if client else 0
    users_billed = client.users_billed if client else 0
    if grok_cost:
        cost = {"posts_billed": grok_cost["x_posts_fetched"], "users_billed": grok_cost["x_users_fetched"],
                "input_tokens": grok_cost["input_tokens"], "output_tokens": grok_cost["output_tokens"],
                "usd_estimate": grok_cost["usd_estimate"]}
    else:
        cost = {"posts_billed": posts_billed, "users_billed": users_billed,
                "usd_estimate": round(posts_billed * PRICE_PER_POST + users_billed * PRICE_PER_USER, 4)}
    sources = {
        "x_api": "X API v2 (official). author verified by user_id. text is verbatim.",
        "free": "Post IDs discovered via X profile embed and/or Grok x_search (URLs only, Grok wording discarded). "
                "Text, author, and time come from X syndication (or X oEmbed fallback), verbatim. "
                "Discovery may miss posts; absence from this feed does not mean the person posted nothing.",
    }
    feed = {
        "schema": "guru-x/v1",
        "generated_at": fetched_at,
        "data_ok": not errors and not budget_stop,
        "provider": provider,
        "source": sources[provider],
        "oembed_selftest": selftest,
        "discovery_stats": grok_stats,
        "discovery_complete": discovery_complete,
        "manual_ids_updated_at": load_json(MANUAL_PATH, {}).get("updated_at"),
        "window_days": cfg.get("window_days", 7),
        "budget_stop": budget_stop,
        "errors": errors,
        "accounts": acc_out,
        "cost_this_run": cost,
        "media_vision": vision_stats,
        "posts": kept,
    }

    write_json(STATE_PATH, state)
    write_json(FEED_PATH, feed)
    new_file = not RUNS_PATH.exists()
    with RUNS_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["generated_at", "provider", "data_ok", "posts_billed", "users_billed", "usd_estimate",
                        "oembed_selftest", "errors"])
        w.writerow([fetched_at, provider, feed["data_ok"], cost["posts_billed"], cost["users_billed"],
                    cost["usd_estimate"], selftest or "", "; ".join(f"{k}: {v}" for k, v in errors.items())])
    return feed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.parse_args()
    feed = run(os.environ.get("X_BEARER_TOKEN") or None, xai_key=os.environ.get("XAI_API_KEY") or None)
    print(json.dumps({k: feed[k] for k in ("generated_at", "provider", "data_ok", "errors", "cost_this_run",
                                           "oembed_selftest", "discovery_stats")},
                     ensure_ascii=False, indent=2))
    print(f"posts in window: {len(feed['posts'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
