"""Mirror the 11:00 report manifest (main + catch-up sets) into the Macro Guru State Store sheet.

GitHub (guru_manifest/data/*.json) stays the canonical copy. The sheet copy exists so the 11:00 report can read the
manifest through the same Sheets connector it already uses for snapshot/log, when a GitHub read fails.

Tabs (created if missing, fully rewritten each build):
  manifest_meta     one row per set (main, C2, C3): status, built_at, expected_records, rids, windows, commit, digest
  manifest_records  one row per record: set, rid, readable columns, record_json (the full record, verbatim)

Write order: meta rows with status=SYNCING -> records -> read back both tabs and verify -> only then the status
column becomes OK -> read back and verify again. A crash before that leaves SYNCING; any difference leaves MISMATCH
and fails the job. The report may use the sheet
copy only when every row it needs says status=OK.

Concurrency: the workflow's concurrency group (guru-manifest, cancel-in-progress false) runs one sync at a time;
a reader that arrives mid-sync sees SYNCING and must not use the mirror.

Usage:
  python guru_manifest/sheet_mirror.py                 # sync (needs GURU_SHEET_ID and GCP_SA_KEY; skips if absent)
  python guru_manifest/sheet_mirror.py --verify META.json [RECORDS.json]
        # offline check: META/RECORDS are the `values` arrays (or {"values": [...]}) read from the two tabs;
        # compares them with the JSON files in this repo and prints a JSON verdict (exit 1 on mismatch).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
REPO = Path(__file__).resolve().parents[1]
MAIN = REPO / "guru_manifest" / "data" / "manifest.json"
CATCHUP = REPO / "guru_manifest" / "data" / "manifest_catchup.json"
META_TAB, REC_TAB = "manifest_meta", "manifest_records"
META_HEADER = ["set", "status", "built_at", "expected_records", "rid_count", "rids",
               "x_lower", "x_upper", "yt_lower", "yt_upper", "web_lower", "web_upper",
               "github_commit", "digest", "synced_at"]
REC_HEADER = ["set", "rid", "person", "role", "tier", "feed", "kind", "at", "url", "same_interview_as",
              "preview", "record_json"]
CELL_MAX = 45000  # Sheets cell limit is 50,000 characters


def canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sets_of(main: dict, catchup: dict) -> list[dict]:
    """[{set, built_at, expected_records, rids, window, records}] in sheet order: main, then C2, C3..."""
    out = [{"set": "main", "built_at": main["built_at"], **{k: main[k] for k in ("expected_records", "rids", "window", "records")}}]
    for s in catchup.get("sets", []):
        out.append({"set": s["set"], "built_at": catchup["built_at"],
                    **{k: s[k] for k in ("expected_records", "rids", "window", "records")}})
    return out


def digest(s: dict) -> str:
    """Fingerprint of what the report depends on: count, rids, windows and every record verbatim."""
    core = {k: s[k] for k in ("set", "built_at", "expected_records", "rids", "window", "records")}
    return hashlib.sha256(canon(core).encode("utf-8")).hexdigest()[:16]


def to_tables(main: dict, catchup: dict, commit: str, status: str, synced_at: str) -> tuple[list[list[str]], list[list[str]]]:
    meta, recs = [list(META_HEADER)], [list(REC_HEADER)]
    for s in sets_of(main, catchup):
        w = s["window"]
        meta.append([s["set"], status, s["built_at"], str(s["expected_records"]), str(len(s["rids"])), " ".join(s["rids"]),
                     w["x"]["lower_exclusive"], w["x"]["upper"], w["yt"]["lower_exclusive"], w["yt"]["upper"],
                     w["web"]["lower_exclusive"], w["web"]["upper"], commit, digest(s), synced_at])
        for r in s["records"]:
            rj = canon(r)
            if len(rj) > CELL_MAX:
                raise ValueError(f"{s['set']} {r['rid']}: record_json {len(rj)} chars exceeds cell limit")
            recs.append([s["set"], r["rid"], str(r.get("person") or ""), str(r.get("role") or ""), str(r.get("tier") or ""),
                         str(r.get("feed") or ""), str(r.get("kind") or ""), str(r.get("at") or ""), str(r.get("url") or ""),
                         " ".join(r.get("same_interview_as") or []), str(r.get("preview") or ""), rj])
    return meta, recs


def _rows(v) -> list[list[str]]:
    if isinstance(v, dict):
        v = v.get("values", [])
    return [[("" if c is None else str(c)) for c in row] for row in (v or [])]


def verify(main: dict, catchup: dict, meta_values, rec_values, require_status: str = "OK") -> dict:
    """Compare the sheet tabs with the canonical JSON. Pure function: no I/O.
    rec_values=None checks manifest_meta only (its digest was computed by the collector, whose own read-back
    compared every record_json); pass the records tab for a full check.
    Returns {"ok": bool, "problems": [...], "sets": {set: {"expected": n, "digest": d, "status": s}}}."""
    meta_only = rec_values is None
    meta, recs = _rows(meta_values), ([list(REC_HEADER)] if meta_only else _rows(rec_values))
    probs: list[str] = []
    if not meta or meta[0][:len(META_HEADER)] != META_HEADER:
        probs.append("manifest_meta header differs")
    if not recs or recs[0][:len(REC_HEADER)] != REC_HEADER:
        probs.append("manifest_records header differs")
    if probs:
        return {"ok": False, "problems": probs, "sets": {}}
    ids = [r[0] for r in meta[1:] if r and r[0]]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        probs.append(f"duplicate meta rows: {' '.join(dup)}")
    mrow = {r[0]: dict(zip(META_HEADER, r + [""] * (len(META_HEADER) - len(r)))) for r in meta[1:] if r and r[0]}
    by_set: dict[str, list[list[str]]] = {}
    for r in recs[1:]:
        if r and r[0]:
            by_set.setdefault(r[0], []).append(r + [""] * (len(REC_HEADER) - len(r)))
    want = sets_of(main, catchup)
    extra = sorted(set(mrow) - {s["set"] for s in want})
    if extra:
        probs.append(f"sheet has sets not in GitHub: {' '.join(extra)}")
    out = {}
    for s in want:
        sid, m = s["set"], mrow.get(s["set"])
        if not m:
            probs.append(f"{sid}: no meta row")
            continue
        if require_status and m["status"] != require_status:
            probs.append(f"{sid}: status={m['status']}")
        w = s["window"]
        exp = {"built_at": s["built_at"], "expected_records": str(s["expected_records"]), "rid_count": str(len(s["rids"])),
               "rids": " ".join(s["rids"]), "x_lower": w["x"]["lower_exclusive"], "x_upper": w["x"]["upper"],
               "yt_lower": w["yt"]["lower_exclusive"], "yt_upper": w["yt"]["upper"],
               "web_lower": w["web"]["lower_exclusive"], "web_upper": w["web"]["upper"], "digest": digest(s)}
        for k, v in exp.items():
            if m[k] != v:
                probs.append(f"{sid}: meta {k} sheet={m[k][:40]!r} github={v[:40]!r}")
        out[sid] = {"expected": s["expected_records"], "digest": digest(s), "status": m["status"]}
        if meta_only:
            continue
        rows = by_set.get(sid, [])
        try:
            got = [json.loads(r[REC_HEADER.index("record_json")]) for r in rows]
        except json.JSONDecodeError:
            probs.append(f"{sid}: record_json not valid JSON")
            got = []
        if [r[1] for r in rows] != s["rids"]:
            probs.append(f"{sid}: record rows rids differ (sheet {len(rows)}, github {len(s['rids'])})")
        elif canon(got) != canon(s["records"]):
            bad = [g.get("rid") for g, e in zip(got, s["records"]) if canon(g) != canon(e)]
            probs.append(f"{sid}: record content differs: {' '.join(map(str, bad[:10]))}")
        else:
            rebuilt = dict(s, records=got)
            if digest(rebuilt) != m["digest"]:
                probs.append(f"{sid}: digest of sheet records differs")
    for sid in sorted(set(by_set) - {s["set"] for s in want}):
        probs.append(f"sheet has record rows for unknown set {sid}")
    return {"ok": not probs, "problems": probs, "sets": out}


class Sheets:
    """Minimal Sheets REST client (service account). Only used in the GitHub Action."""

    def __init__(self, sheet_id: str, sa_info: dict):
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        creds = service_account.Credentials.from_service_account_info(
            sa_info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self.s = AuthorizedSession(creds)
        self.base = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"

    def _ok(self, r):
        if r.status_code >= 300:
            raise RuntimeError(f"Sheets API {r.status_code}: {r.text[:300]}")
        return r.json()

    def ensure_tabs(self, names: list[str]) -> None:
        have = {s["properties"]["title"] for s in self._ok(self.s.get(self.base, params={"fields": "sheets.properties.title"}))["sheets"]}
        add = [{"addSheet": {"properties": {"title": n}}} for n in names if n not in have]
        if add:
            self._ok(self.s.post(f"{self.base}:batchUpdate", json={"requests": add}))

    def clear(self, rng: str) -> None:
        self._ok(self.s.post(f"{self.base}/values:batchClear", json={"ranges": [rng]}))

    def put(self, rng: str, values: list[list[str]]) -> None:
        self._ok(self.s.put(f"{self.base}/values/{rng}", params={"valueInputOption": "RAW"},
                            json={"range": rng, "majorDimension": "ROWS", "values": values}))

    def get(self, rng: str) -> list[list[str]]:
        return self._ok(self.s.get(f"{self.base}/values/{rng}", params={"valueRenderOption": "UNFORMATTED_VALUE"})).get("values", [])


def sync(client, main: dict, catchup: dict, commit: str, now: str) -> dict:
    """status=OK is written only after the full read-back matched. A crash at any point before that leaves SYNCING
    (never OK) on every set; a failed check leaves MISMATCH."""
    client.ensure_tabs([META_TAB, REC_TAB])
    meta_sync, recs = to_tables(main, catchup, commit, "SYNCING", now)
    client.clear(f"{META_TAB}!A:O")
    client.put(f"{META_TAB}!A1", meta_sync)          # readers see SYNCING while records are rewritten
    client.clear(f"{REC_TAB}!A:L")
    client.put(f"{REC_TAB}!A1", recs)
    v = verify(main, catchup, client.get(f"{META_TAB}!A:O"), client.get(f"{REC_TAB}!A:L"), require_status="SYNCING")
    n = len(meta_sync) - 1
    if v["ok"]:
        client.put(f"{META_TAB}!B2:B{n + 1}", [["OK"]] * n)   # only the status column changes
        v = verify(main, catchup, client.get(f"{META_TAB}!A:O"), client.get(f"{REC_TAB}!A:L"), require_status="OK")
    if not v["ok"]:
        client.put(f"{META_TAB}!B2:B{n + 1}", [["MISMATCH"]] * n)
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", nargs="+", metavar="JSON", help="META_JSON [RECORDS_JSON]")
    ap.add_argument("--allow-status", default="OK", help="meta status required in --verify mode ('' = any)")
    a = ap.parse_args()
    main_j, catch_j = json.loads(MAIN.read_text(encoding="utf-8")), json.loads(CATCHUP.read_text(encoding="utf-8"))
    if a.verify:
        mv = json.loads(Path(a.verify[0]).read_text(encoding="utf-8"))
        rv = json.loads(Path(a.verify[1]).read_text(encoding="utf-8")) if len(a.verify) > 1 else None
        v = verify(main_j, catch_j, mv, rv, a.allow_status)
        print(json.dumps(v, ensure_ascii=False, indent=1))
        return 0 if v["ok"] else 1
    sid, key = os.environ.get("GURU_SHEET_ID"), os.environ.get("GCP_SA_KEY")
    if not sid or not key:
        print("GURU_SHEET_ID / GCP_SA_KEY not set; sheet mirror skipped (GitHub manifest unaffected)")
        return 0
    commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    v = sync(Sheets(sid, json.loads(key)), main_j, catch_j, commit, datetime.now(KST).isoformat(timespec="seconds"))
    print(json.dumps(v, ensure_ascii=False, indent=1))
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
