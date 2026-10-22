"""Dispatcher tick. An external cron (cron-job.org) calls this workflow every 5 minutes; GitHub's own
schedule for this repo is unreliable. For every target slot that came due within catch_up_minutes and
has no run of that workflow (and mode) created at or after the slot, send one workflow_dispatch.

Tokens: GITHUB_TOKEN for this repo, DISPATCH_TOKEN (fine-grained PAT, Actions read/write) for other repos.
Tokens are only ever sent in the Authorization header and never printed."""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
HERE = Path(__file__).parent
SELF_REPO = os.environ.get("GITHUB_REPOSITORY", "jaysallr-ux/Semis-radar")


def field_match(spec: str, value: int, lo: int, hi: int) -> bool:
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-"))
        else:
            a = b = int(part)
        if a <= value <= b and (value - a) % step == 0:
            return True
    return False


def cron_match(expr: str, t: datetime) -> bool:
    mi, ho, dom, mon, dow = expr.split()
    wd = (t.weekday() + 1) % 7  # Python Mon=0 -> cron Sun=0
    return (field_match(mi, t.minute, 0, 59) and field_match(ho, t.hour, 0, 23)
            and field_match(dom, t.day, 1, 31) and field_match(mon, t.month, 1, 12)
            and (field_match(dow, wd, 0, 7) or (wd == 0 and field_match(dow, 7, 0, 7))))


def latest_slot(expr: str, now: datetime, window_min: int) -> datetime | None:
    t = now.replace(second=0, microsecond=0)
    for _ in range(window_min + 1):
        if cron_match(expr, t):
            return t
        t -= timedelta(minutes=1)
    return None


def api(method: str, url: str, token: str, body: dict | None = None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None


def main() -> int:
    cfg = json.loads((HERE / "schedule.json").read_text(encoding="utf-8"))
    now = datetime.now(KST)
    self_tok = os.environ.get("GH_TOKEN_SELF", "")
    other_tok = os.environ.get("DISPATCH_TOKEN", "")
    lines, failed = [], False
    for tg in cfg["targets"]:
        slot = latest_slot(tg["cron"], now, cfg.get("catch_up_minutes", 90))
        if not slot:
            continue
        tok = self_tok if tg["repo"] == SELF_REPO else other_tok
        if not tok:
            lines.append(f"SKIP  {tg['name']}: no token for {tg['repo']} (add DISPATCH_TOKEN secret)")
            continue
        base = f"https://api.github.com/repos/{tg['repo']}/actions/workflows/{tg['workflow']}"
        # GitHub keeps only one pending run per concurrency group and cancels the older pending one when a
        # new run is queued. So never dispatch while ANY run of this workflow (any mode) is still active:
        # wait for the next tick instead, or a discover dispatch could cancel a pending full run.
        busy = False
        for status in ("queued", "in_progress", "pending", "waiting", "requested"):
            st, data = api("GET", f"{base}/runs?status={status}&per_page=1", tok)
            if st == 200 and data.get("total_count"):
                busy = True
                break
        if busy:
            lines.append(f"WAIT  {tg['name']}: a run of {tg['workflow']} is still active")
            continue
        since = slot.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        st, data = api("GET", f"{base}/runs?created=%3E%3D{since}&per_page=30", tok)
        if st != 200:
            lines.append(f"ERROR {tg['name']}: list runs HTTP {st}")
            failed = True
            continue
        runs = data.get("workflow_runs", [])
        suf = tg.get("title_suffix")
        if suf:
            runs = [r for r in runs if (r.get("display_title") or "").strip().endswith(suf)]
        ok_runs = [r for r in runs if r.get("conclusion") in ("success", "skipped", "neutral")]
        fails = sum(1 for r in runs if r.get("conclusion") in ("failure", "cancelled", "timed_out", "startup_failure"))
        if ok_runs:
            continue  # already covered (running, queued or done) since this slot
        if fails >= 3:
            lines.append(f"GIVEUP {tg['name']}: {fails} failed runs since {slot:%H:%M}")
            continue
        body = {"ref": tg.get("ref", "main")}
        if tg.get("inputs"):
            body["inputs"] = tg["inputs"]
        st, _ = api("POST", f"{base}/dispatches", tok, body)
        lines.append(f"{'SENT ' if st == 204 else 'ERROR'} {tg['name']}: slot {slot:%m-%d %H:%M} KST -> HTTP {st}"
                     + (f" (retry after {fails} failure)" if fails else ""))
        failed |= st != 204
    out = "\n".join(lines) or f"nothing due at {now:%H:%M} KST"
    print(out)
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        Path(summ).write_text("```\n" + out + "\n```\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
