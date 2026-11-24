"""Tests for sheet_mirror.py. Run: python -m pytest guru_manifest/test_sheet_mirror.py -q"""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sheet_mirror as sm  # noqa: E402


@pytest.fixture
def data():
    return (json.loads(sm.MAIN.read_text(encoding="utf-8")), json.loads(sm.CATCHUP.read_text(encoding="utf-8")))


class FakeSheets:
    """In-memory sheet that behaves like values.get: strings only, trailing empty cells and rows dropped."""

    def __init__(self, mangle=None, fail_on_partial=False):
        self.tabs, self.calls, self.mangle, self.fail_on_partial = {}, [], mangle, fail_on_partial

    def ensure_tabs(self, names):
        for n in names:
            self.tabs.setdefault(n, [])

    def clear(self, rng):
        self.calls.append(("clear", rng))
        self.tabs[rng.split("!")[0]] = []

    def put(self, rng, values):
        tab, a1 = rng.split("!")
        rows = [[str(c) for c in r] for r in values]
        if self.mangle:
            rows = self.mangle(tab, rows)
        if a1 == "A1":
            self.calls.append(("put", tab, rows[1][1] if len(rows) > 1 and tab == sm.META_TAB else None))
            self.tabs[tab] = rows
            return
        # partial write such as B2:B4 (status column)
        if self.fail_on_partial:
            raise RuntimeError("simulated crash")
        start = a1.split(":")[0]
        col, row = ord(start[0]) - 65, int(start[1:]) - 1
        self.calls.append(("put", tab, rows[0][0]))
        for i, r in enumerate(rows):
            for j, v in enumerate(r):
                tgt = self.tabs[tab][row + i]
                while len(tgt) <= col + j:
                    tgt.append("")
                tgt[col + j] = v

    def get(self, rng):
        out = []
        for r in self.tabs.get(rng.split("!")[0], []):
            r = list(r)
            while r and r[-1] == "":
                r.pop()
            out.append(r)
        return out


def test_roundtrip_ok(data):
    m, c = data
    f = FakeSheets()
    v = sm.sync(f, m, c, "abc123", "2026-11-24T10:41:00+09:00")
    assert v["ok"], v["problems"]
    assert set(v["sets"]) == {"main"} | {s["set"] for s in c["sets"]}
    assert v["sets"]["main"]["expected"] == m["expected_records"]
    assert all(r[1] == "OK" for r in f.tabs[sm.META_TAB][1:])
    assert len(f.tabs[sm.REC_TAB]) - 1 == m["expected_records"] + sum(s["expected_records"] for s in c["sets"])


def test_syncing_written_before_records(data):
    m, c = data
    f = FakeSheets()
    sm.sync(f, m, c, "abc", "t")
    puts = [x for x in f.calls if x[0] == "put"]
    assert puts[0][1:] == (sm.META_TAB, "SYNCING")
    assert puts[1][1] == sm.REC_TAB
    assert puts[2][1:] == (sm.META_TAB, "OK")      # status column only, after the first verified read-back


def test_crash_before_ok_leaves_syncing(data):
    m, c = data
    f = FakeSheets(fail_on_partial=True)
    with pytest.raises(RuntimeError):
        sm.sync(f, m, c, "abc", "t")
    assert all(r[1] == "SYNCING" for r in f.tabs[sm.META_TAB][1:])
    assert not sm.verify(m, c, f.get(sm.META_TAB + "!A:O"), f.get(sm.REC_TAB + "!A:L"))["ok"]


def test_ok_never_written_when_first_check_fails(data):
    m, c = data

    def drop_last_record(tab, rows):
        return rows[:-1] if tab == sm.REC_TAB else rows

    f = FakeSheets(mangle=drop_last_record)
    sm.sync(f, m, c, "abc", "t")
    assert ("put", sm.META_TAB, "OK") not in f.calls


def test_corrupted_write_marks_mismatch(data):
    m, c = data

    def drop_last_record(tab, rows):
        return rows[:-1] if tab == sm.REC_TAB else rows

    f = FakeSheets(mangle=drop_last_record)
    v = sm.sync(f, m, c, "abc", "t")
    assert not v["ok"]
    assert all(r[1] == "MISMATCH" for r in f.tabs[sm.META_TAB][1:])


def test_detects_record_content_change(data):
    m, c = data
    meta, recs = sm.to_tables(m, c, "abc", "OK", "t")
    j = json.loads(recs[1][-1]); j["person"] = "Somebody Else"; recs[1][-1] = sm.canon(j)
    v = sm.verify(m, c, meta, recs)
    assert not v["ok"] and any("record content differs: R001" in p for p in v["problems"])


def test_detects_expected_and_window_change(data):
    m, c = data
    meta, recs = sm.to_tables(m, c, "abc", "OK", "t")
    meta[1][3] = str(m["expected_records"] + 1)
    meta[1][6] = "2026-01-01T00:00:00+09:00"
    probs = " ".join(sm.verify(m, c, meta, recs)["problems"])
    assert "expected_records" in probs and "x_lower" in probs


def test_rejects_non_ok_status(data):
    m, c = data
    meta, recs = sm.to_tables(m, c, "abc", "SYNCING", "t")
    v = sm.verify(m, c, meta, recs)
    assert not v["ok"] and any("status=SYNCING" in p for p in v["problems"])
    assert sm.verify(m, c, meta, recs, require_status="")["ok"]


def test_stale_sheet_from_previous_day(data):
    m, c = data
    old_m = copy.deepcopy(m); old_m["built_at"] = "2026-11-23T10:40:00+09:00"
    meta, recs = sm.to_tables(old_m, c, "abc", "OK", "t")
    v = sm.verify(m, c, meta, recs)
    assert not v["ok"] and any("main: meta built_at" in p for p in v["problems"])


def test_missing_and_extra_sets(data):
    m, c = data
    meta, recs = sm.to_tables(m, c, "abc", "OK", "t")
    meta_missing = [r for r in meta if r[0] != "C2"]
    assert any("C2: no meta row" in p for p in sm.verify(m, c, meta_missing, recs)["problems"])
    meta_extra = meta + [["C9"] + meta[1][1:]]
    assert any("not in GitHub: C9" in p for p in sm.verify(m, c, meta_extra, recs)["problems"])


def test_header_change_fails(data):
    m, c = data
    meta, recs = sm.to_tables(m, c, "abc", "OK", "t")
    meta[0][1] = "state"
    assert not sm.verify(m, c, meta, recs)["ok"]


def test_cli_verify(tmp_path, data):
    m, c = data
    meta, recs = sm.to_tables(m, c, "abc", "OK", "t")
    (tmp_path / "meta.json").write_text(json.dumps({"values": meta}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "recs.json").write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([sys.executable, str(Path(sm.__file__)), "--verify", str(tmp_path / "meta.json"), str(tmp_path / "recs.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)["ok"]
    recs[2][1] = "R999"
    (tmp_path / "recs.json").write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([sys.executable, str(Path(sm.__file__)), "--verify", str(tmp_path / "meta.json"), str(tmp_path / "recs.json")],
                       capture_output=True, text=True)
    assert r.returncode == 1


def test_sync_skips_without_secrets(monkeypatch):
    monkeypatch.delenv("GURU_SHEET_ID", raising=False)
    monkeypatch.delenv("GCP_SA_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["sheet_mirror.py"])
    assert sm.main() == 0


def test_cell_limit(data):
    m, c = data
    big = copy.deepcopy(m); big["records"][0]["preview"] = "x" * (sm.CELL_MAX + 1)
    with pytest.raises(ValueError):
        sm.to_tables(big, c, "abc", "OK", "t")


def test_meta_only(data):
    m, c = data
    meta, _ = sm.to_tables(m, c, "abc", "OK", "t")
    assert sm.verify(m, c, meta, None)["ok"]
    meta[1][13] = "0" * 16
    assert any("digest" in p for p in sm.verify(m, c, meta, None)["problems"])
    meta2, _ = sm.to_tables(m, c, "abc", "MISMATCH", "t")
    assert not sm.verify(m, c, meta2, None)["ok"]


def test_duplicate_meta_rows(data):
    m, c = data
    meta, recs = sm.to_tables(m, c, "abc", "OK", "t")
    meta.append(list(meta[1]))
    assert any("duplicate meta rows: main" in p for p in sm.verify(m, c, meta, recs)["problems"])
    assert any("duplicate meta rows" in p for p in sm.verify(m, c, meta, None)["problems"])


def test_duplicate_record_rows(data):
    m, c = data
    meta, recs = sm.to_tables(m, c, "abc", "OK", "t")
    recs.insert(2, list(recs[1]))
    assert not sm.verify(m, c, meta, recs)["ok"]
