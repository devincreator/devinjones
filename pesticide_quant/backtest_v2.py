#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PIT walk-forward backtest v2 with interval-level production gate."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pandas as pd

import backtest as base

ACCEPTED_J003 = {"LOADED", "LOADED_OFFICIAL_WITH_SNAPSHOT_DIFF"}


def table_exists(con, name):
    return con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0] > 0


def gate_v2(con, m, mem, fin):
    row = con.execute("SELECT status,note FROM ingestion_job WHERE job_id='J003'").fetchone()
    j003 = row[0] if row else None
    j003_note = row[1] if row else None

    if m.empty:
        market_start = market_end = None
        required = mem.iloc[0:0].copy()
    else:
        market_start = m["trade_date"].min()
        market_end = m["trade_date"].max()
        required = mem[
            (mem["in_date"] <= market_end)
            & (mem["out_date"].isna() | (mem["out_date"] >= market_start))
        ].copy()

    required_codes = set(required["code"].astype(str))
    market_codes = set(m["code"].astype(str))
    finance_codes = set(fin["code"].astype(str)) if not fin.empty else set()
    missing_market = sorted(required_codes - market_codes)
    missing_finance = sorted(required_codes - finance_codes)

    interval_audit_exists = table_exists(con, "market_interval_coverage")
    failed_interval_codes = []
    interval_rows = 0
    if interval_audit_exists:
        interval_rows = con.execute("SELECT COUNT(*) FROM market_interval_coverage").fetchone()[0]
        failed_interval_codes = [r[0] for r in con.execute(
            "SELECT DISTINCT code FROM market_interval_coverage WHERE coverage_ok=0 ORDER BY code"
        )]

    g = {
        "j003_status": j003,
        "j003_note": j003_note,
        "membership_rows": int(len(mem)),
        "membership_codes": int(mem["code"].nunique()) if not mem.empty else 0,
        "market_rows": int(len(m)),
        "market_codes": int(m["code"].nunique()) if not m.empty else 0,
        "finance_rows": int(len(fin)),
        "finance_codes": int(fin["code"].nunique()) if not fin.empty else 0,
        "market_start": None if market_start is None else market_start.date().isoformat(),
        "market_end": None if market_end is None else market_end.date().isoformat(),
        "required_membership_codes_in_market_window": len(required_codes),
        "missing_required_market_codes": missing_market,
        "missing_required_finance_codes": missing_finance,
        "interval_audit_exists": interval_audit_exists,
        "interval_audit_rows": int(interval_rows),
        "failed_interval_coverage_codes": failed_interval_codes,
    }
    g["formal_ok"] = bool(
        j003 in ACCEPTED_J003
        and len(mem) > 0
        and len(m) > 5000
        and len(required_codes) > 0
        and not missing_market
        and not missing_finance
        and interval_audit_exists
        and interval_rows > 0
        and not failed_interval_codes
    )
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--allow-partial", action="store_true")
    args = ap.parse_args()

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(args.db)
    try:
        m, mem, fin = base.load(con)
        g = gate_v2(con, m, mem, fin)
        (out / "gate.json").write_text(json.dumps(g, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(g, ensure_ascii=False, indent=2))

        if not g["formal_ok"] and not args.allow_partial:
            raise SystemExit(2)
        if mem.empty or m.empty:
            raise SystemExit(3)

        tf = base.tech_features(m)
        feat = base.feature_dates(tf, mem)
        feat = base.join_fin(feat, fin)
        panel = base.labels(feat, m)
        metrics, preds = base.walk(panel)
        agg = base.aggregate(preds)

        panel.to_csv(out / "pit_panel.csv", index=False, encoding="utf-8-sig")
        metrics.to_csv(out / "walk_forward_metrics_by_year.csv", index=False, encoding="utf-8-sig")
        agg.to_csv(out / "walk_forward_metrics_overall.csv", index=False, encoding="utf-8-sig")
        preds.to_csv(out / "walk_forward_predictions.csv", index=False, encoding="utf-8-sig")

        summary = {
            "status": "FORMAL_TECH_FIN" if g["formal_ok"] else "PARTIAL_TECH_FIN",
            "scope_note": "TECH/FIN baseline only; capital-flow and industry-cycle modules are not yet included",
            "gate": g,
            "panel_rows": int(len(panel)),
            "panel_codes": int(panel["code"].nunique()) if not panel.empty else 0,
            "aggregate": agg.to_dict("records"),
        }
        (out / "backtest_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        con.close()


if __name__ == "__main__":
    main()
