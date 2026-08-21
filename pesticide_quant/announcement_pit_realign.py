#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repair ANN_CHAIN_TITLE_V1 effective dates and rebuild daily announcement features.

Why this exists
---------------
The first V1 implementation mapped each announcement to the next date available
in that *stock's* market history.  If the local market archive began long after
the announcement, an old disclosure could therefore be injected at the first
sample date months or years later.  That is a PIT error.

Correct rule
------------
Public information becomes usable on the next *market-wide* observed trading
session after notice_date.  If the archive cannot identify a nearby next
session, effective_date is left NULL rather than manufacturing a date.

The script then rebuilds announcement_feature_daily from the corrected event
calendar using announcement_intelligence.score_daily().
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

import announcement_intelligence as ann


def strict_next_market_day(calendar: pd.DatetimeIndex, notice, max_gap_days: int):
    nd = pd.Timestamp(notice).normalize()
    # Deliberately prohibit same-day use.  Even a morning/afternoon timestamp
    # ambiguity cannot leak information into the notice-date close.
    i = calendar.searchsorted(nd + pd.Timedelta(days=1), side="left")
    if i >= len(calendar):
        return None
    eff = pd.Timestamp(calendar[i]).normalize()
    gap = int((eff - nd).days)
    if gap <= 0 or gap > max_gap_days:
        return None
    return eff


def gap_days(effective, notice):
    if pd.isna(effective) or pd.isna(notice):
        return np.nan
    return float((pd.Timestamp(effective).normalize() - pd.Timestamp(notice).normalize()).days)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--audit-json")
    ap.add_argument("--max-gap-days", type=int, default=15)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    try:
        market = pd.read_sql_query(
            """SELECT trade_date,code,close_qfq FROM market_daily
               WHERE close_qfq IS NOT NULL ORDER BY code,trade_date""", con
        )
        membership = pd.read_sql_query(
            "SELECT code,in_date,out_date FROM industry_membership_history", con
        )
        events = pd.read_sql_query(
            "SELECT * FROM announcement_event ORDER BY canonical_code,notice_date,art_code", con
        )
        if market.empty or events.empty:
            raise SystemExit("market or announcement_event is empty")

        market["trade_date"] = pd.to_datetime(market["trade_date"])
        events["notice_date"] = pd.to_datetime(events["notice_date"], errors="coerce")
        old_eff = pd.to_datetime(events["effective_date"], errors="coerce")
        calendar = pd.DatetimeIndex(sorted(market["trade_date"].dropna().dt.normalize().unique()))
        if calendar.empty:
            raise SystemExit("market-wide trading calendar is empty")

        old_gap = pd.Series(
            [gap_days(e, n) for e, n in zip(old_eff, events["notice_date"])],
            index=events.index,
            dtype=float,
        )
        old_bad_gap = old_gap.gt(args.max_gap_days)
        old_same_or_before = old_gap.le(0)

        new_eff = []
        for nd in events["notice_date"]:
            if pd.isna(nd):
                new_eff.append(pd.NaT)
            else:
                v = strict_next_market_day(calendar, nd, args.max_gap_days)
                new_eff.append(pd.NaT if v is None else v)
        new_eff = pd.Series(pd.to_datetime(new_eff), index=events.index)
        new_gap = pd.Series(
            [gap_days(e, n) for e, n in zip(new_eff, events["notice_date"])],
            index=events.index,
            dtype=float,
        )

        old_norm = old_eff.dt.normalize()
        new_norm = new_eff.dt.normalize()
        changed = ~(old_norm.fillna(pd.Timestamp("1900-01-01")) ==
                    new_norm.fillna(pd.Timestamp("1900-01-01")))
        null_due_calendar = new_eff.isna()
        new_bad_gap = new_gap.gt(args.max_gap_days)
        new_same_or_before = new_gap.le(0)

        # Persist corrected event timing in one transaction.
        upd = []
        for r, eff in zip(events.itertuples(index=False), new_eff):
            upd.append((None if pd.isna(eff) else eff.date().isoformat(),
                        r.canonical_code, r.art_code))
        con.executemany(
            "UPDATE announcement_event SET effective_date=? WHERE canonical_code=? AND art_code=?",
            upd,
        )

        # Re-read so score_daily sees exactly what is persisted.
        fixed = pd.read_sql_query(
            "SELECT * FROM announcement_event ORDER BY canonical_code,notice_date,art_code", con
        )
        features = ann.score_daily(fixed, market, membership)
        con.execute("DELETE FROM announcement_feature_daily WHERE source_id=?", (ann.SOURCE_ID,))
        if not features.empty:
            features.to_sql("announcement_feature_daily", con, if_exists="append", index=False)
        con.commit()

        # Hard PIT audits.
        fixed_eff = pd.to_datetime(fixed["effective_date"], errors="coerce")
        fixed_notice = pd.to_datetime(fixed["notice_date"], errors="coerce")
        fixed_gap = (fixed_eff - fixed_notice).dt.days
        audit = {
            "rule": "next market-wide observed trading session; never notice-date; NULL if gap exceeds bound",
            "max_gap_days": args.max_gap_days,
            "calendar_min": calendar.min().date().isoformat(),
            "calendar_max": calendar.max().date().isoformat(),
            "events": int(len(events)),
            "old_effective_nonnull": int(old_eff.notna().sum()),
            "old_bad_gap_gt_bound": int(old_bad_gap.sum()),
            "old_same_or_before_notice": int(old_same_or_before.sum()),
            "changed_effective_date": int(changed.sum()),
            "corrected_effective_nonnull": int(new_eff.notna().sum()),
            "corrected_effective_null": int(null_due_calendar.sum()),
            "corrected_bad_gap_gt_bound": int(new_bad_gap.sum()),
            "corrected_same_or_before_notice": int(new_same_or_before.sum()),
            "classified_events_null_after_fix": int(((events["category"] != "OTHER") & null_due_calendar).sum()),
            "feature_rows_rebuilt": int(len(features)),
            "feature_codes_rebuilt": int(features["code"].nunique()) if len(features) else 0,
            "persisted_bad_gap_gt_bound": int((fixed_gap > args.max_gap_days).sum()),
            "persisted_same_or_before_notice": int((fixed_gap <= 0).sum()),
        }
        if args.audit_json:
            Path(args.audit_json).write_text(
                json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(json.dumps(audit, ensure_ascii=False, indent=2))

        if audit["persisted_bad_gap_gt_bound"] or audit["persisted_same_or_before_notice"]:
            raise SystemExit(2)
    finally:
        con.close()


if __name__ == "__main__":
    main()
