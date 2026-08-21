#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interval-level market coverage audit for official pesticide membership.

A code-level existence check is insufficient: a stock can have only recent bars
while its official pesticide interval began years earlier. This audit checks the
first/last covered trading session for every membership interval overlapping the
backtest sample.

Some SW classification start dates precede the actual IPO by several sessions.
To avoid treating this metadata lead as a data gap, SH/SZ intervals allow up to
25 market sessions at an edge. Beijing eligibility is explicitly bounded:
- 920819: 2021-11-15 (BSE launch; predecessor 833819 was NEEQ before this)
- 920866: 2022-12-09 (listing)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pandas as pd

SAMPLE_START = pd.Timestamp("2020-06-01")
BSE_LISTING = {
    "920819": pd.Timestamp("2021-11-15"),
    "920866": pd.Timestamp("2022-12-09"),
}
MAX_EDGE_GAP_SESSIONS = 25
MIN_ROW_RATIO = 0.50


def common_calendar(con: sqlite3.Connection) -> pd.DatetimeIndex:
    x = pd.read_sql_query(
        """SELECT trade_date,COUNT(DISTINCT code) AS n
           FROM market_daily WHERE close_qfq IS NOT NULL
           GROUP BY trade_date ORDER BY trade_date""", con
    )
    x["trade_date"] = pd.to_datetime(x["trade_date"])
    dates = x.loc[x["n"] >= 10, "trade_date"]
    if dates.empty:
        raise RuntimeError("cannot build common market calendar")
    return pd.DatetimeIndex(dates)


def count_sessions(cal, start, end):
    if pd.isna(start) or pd.isna(end) or start > end:
        return 0
    return int(((cal >= start) & (cal <= end)).sum())


def audit(db: str, out_csv: str, out_json: str):
    con = sqlite3.connect(db)
    try:
        mem = pd.read_sql_query(
            "SELECT code,in_date,out_date FROM industry_membership_history ORDER BY code,in_date", con
        )
        mem["in_date"] = pd.to_datetime(mem["in_date"])
        mem["out_date"] = pd.to_datetime(mem["out_date"], errors="coerce")
        cal = common_calendar(con)
        sample_end = cal.max()

        rows = []
        for r in mem.itertuples(index=False):
            interval_end = sample_end if pd.isna(r.out_date) else min(r.out_date, sample_end)
            req_start = max(r.in_date, SAMPLE_START)
            lifecycle_source = "SW_START_WITH_25_SESSION_TOLERANCE"
            if str(r.code) in BSE_LISTING:
                req_start = max(req_start, BSE_LISTING[str(r.code)])
                lifecycle_source = "BSE_ELIGIBILITY_RULE"
            req_end = interval_end
            if req_start > req_end:
                continue

            bars = pd.read_sql_query(
                """SELECT trade_date FROM market_daily
                   WHERE code=? AND close_qfq IS NOT NULL AND trade_date>=? AND trade_date<=?
                   ORDER BY trade_date""",
                con,
                params=(str(r.code), req_start.date().isoformat(), req_end.date().isoformat()),
            )
            bars["trade_date"] = pd.to_datetime(bars["trade_date"])
            first = bars["trade_date"].min() if not bars.empty else pd.NaT
            last = bars["trade_date"].max() if not bars.empty else pd.NaT
            expected_sessions = count_sessions(cal, req_start, req_end)
            row_ratio = (len(bars) / expected_sessions) if expected_sessions else 1.0
            leading = expected_sessions if pd.isna(first) else count_sessions(cal, req_start, first) - 1
            trailing = expected_sessions if pd.isna(last) else count_sessions(cal, last, req_end) - 1
            ok = bool(
                len(bars) > 0
                and leading <= MAX_EDGE_GAP_SESSIONS
                and trailing <= MAX_EDGE_GAP_SESSIONS
                and row_ratio >= MIN_ROW_RATIO
            )
            rows.append({
                "code": str(r.code),
                "membership_in": r.in_date.date().isoformat(),
                "membership_out": "" if pd.isna(r.out_date) else r.out_date.date().isoformat(),
                "list_date": BSE_LISTING.get(str(r.code), pd.NaT).date().isoformat() if str(r.code) in BSE_LISTING else "",
                "delist_date": "",
                "lifecycle_source": lifecycle_source,
                "required_start": req_start.date().isoformat(),
                "required_end": req_end.date().isoformat(),
                "first_market": "" if pd.isna(first) else first.date().isoformat(),
                "last_market": "" if pd.isna(last) else last.date().isoformat(),
                "expected_sessions": expected_sessions,
                "market_rows": int(len(bars)),
                "row_ratio": row_ratio,
                "leading_gap_sessions": int(leading),
                "trailing_gap_sessions": int(trailing),
                "coverage_ok": int(ok),
            })

        df = pd.DataFrame(rows)
        Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_csv, index=False, encoding="utf-8-sig")
        failed = df[df["coverage_ok"] == 0].copy()
        summary = {
            "sample_start": SAMPLE_START.date().isoformat(),
            "sample_end": sample_end.date().isoformat(),
            "intervals_audited": int(len(df)),
            "intervals_ok": int((df["coverage_ok"] == 1).sum()),
            "intervals_failed": int((df["coverage_ok"] == 0).sum()),
            "failed_codes": sorted(failed["code"].unique().tolist()),
            "max_edge_gap_sessions": MAX_EDGE_GAP_SESSIONS,
            "min_row_ratio": MIN_ROW_RATIO,
        }
        Path(out_json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        con.execute("""CREATE TABLE IF NOT EXISTS market_interval_coverage(
            code TEXT NOT NULL,membership_in TEXT NOT NULL,membership_out TEXT,
            list_date TEXT,delist_date TEXT,lifecycle_source TEXT,required_start TEXT,required_end TEXT,
            first_market TEXT,last_market TEXT,expected_sessions INTEGER,market_rows INTEGER,row_ratio REAL,
            leading_gap_sessions INTEGER,trailing_gap_sessions INTEGER,coverage_ok INTEGER NOT NULL,
            PRIMARY KEY(code,membership_in))""")
        con.execute("DELETE FROM market_interval_coverage")
        con.executemany(
            """INSERT INTO market_interval_coverage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [tuple(x) for x in df[[
                "code","membership_in","membership_out","list_date","delist_date","lifecycle_source",
                "required_start","required_end","first_market","last_market","expected_sessions","market_rows",
                "row_ratio","leading_gap_sessions","trailing_gap_sessions","coverage_ok"
            ]].itertuples(index=False, name=None)],
        )
        status = "COVERAGE_OK" if not summary["failed_codes"] else "COVERAGE_GAPS"
        con.execute(
            "UPDATE ingestion_job SET status=?,note=COALESCE(note,'')||? WHERE job_id='J001'",
            (status, f"; interval audit failed_codes={summary['failed_codes']}"),
        )
        con.commit()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if summary["failed_codes"]:
            print(failed.to_string(index=False))
            raise SystemExit(2)
    finally:
        con.close()


def main():
    ap=argparse.ArgumentParser();ap.add_argument("--db",required=True);ap.add_argument("--out-csv",default="work/market_interval_coverage.csv");ap.add_argument("--out-json",default="work/market_interval_coverage.json");a=ap.parse_args()
    audit(a.db,a.out_csv,a.out_json)


if __name__=="__main__":main()
