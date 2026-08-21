#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-pass PIT + semantic rebuild for title-level announcement V1.1.

Combines the two non-negotiable corrections before feature construction:
1) effective_date is the next market-wide observed trading session, never the
   first much-later date in one stock's truncated history;
2) a small set of keyword-precedence errors are corrected conservatively.

The original V1 scorer is then run exactly once on the corrected event table.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pandas as pd

import announcement_intelligence as ann

VERSION = "ANN_CHAIN_TITLE_V1_1_PIT_SEMANTIC"


def next_market_date(calendar: pd.DatetimeIndex, notice, max_gap_days: int):
    nd = pd.Timestamp(notice).normalize()
    i = calendar.searchsorted(nd + pd.Timedelta(days=1), side="left")
    if i >= len(calendar):
        return None
    eff = pd.Timestamp(calendar[i]).normalize()
    gap = int((eff - nd).days)
    return eff if 0 < gap <= max_gap_days else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--max-gap-days", type=int, default=15)
    ap.add_argument("--audit-json")
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
        ev = pd.read_sql_query(
            "SELECT * FROM announcement_event ORDER BY canonical_code,notice_date,art_code", con
        )
        if market.empty or ev.empty:
            raise SystemExit("market or announcement_event is empty")
        market["trade_date"] = pd.to_datetime(market["trade_date"])
        ev["notice_date"] = pd.to_datetime(ev["notice_date"], errors="coerce")
        old_eff = pd.to_datetime(ev["effective_date"], errors="coerce")
        calendar = pd.DatetimeIndex(sorted(market["trade_date"].dropna().dt.normalize().unique()))

        old_gap = (old_eff - ev["notice_date"]).dt.days
        new_eff = []
        for nd in ev["notice_date"]:
            if pd.isna(nd):
                new_eff.append(pd.NaT)
            else:
                x = next_market_date(calendar, nd, args.max_gap_days)
                new_eff.append(pd.NaT if x is None else x)
        ev["effective_date"] = pd.to_datetime(new_eff)

        # Specific semantics must beat generic substring rules.
        masks = {}
        masks["inquiry_reply_negative_to_neutral"] = (
            ev["category"].eq("INQUIRY") & ev["title"].str.contains(
                r"回复问询函|问询函回复|问询函的回复|问询函的回函|回复关注函|关注函回复|监管工作函回复",
                regex=True, na=False,
            )
        )
        masks["pledge_to_release"] = (
            ev["category"].eq("PLEDGE") & ev["title"].str.contains(
                r"解除股份质押|解除质押", regex=True, na=False,
            )
        )
        masks["shutdown_to_recovery"] = (
            ev["category"].eq("RISK") & ev["title"].str.contains(
                r"复产|恢复生产|解除停产|整改完成", regex=True, na=False,
            )
        )
        masks["buyback_procedural_to_neutral"] = (
            ev["category"].eq("BUYBACK") & ev["title"].str.contains(
                r"回购股份事项前十大股东|回购股份事项前十名股东|前十大无限售|前十名无限售",
                regex=True, na=False,
            )
        )

        specs = {
            "inquiry_reply_negative_to_neutral": ("INQUIRY_REPLY", 2, 0, 1.0, "回复函"),
            "pledge_to_release": ("PLEDGE_RECOVERY", 5, 1, 2.0, "解除质押"),
            "shutdown_to_recovery": ("RISK_RECOVERY", 7, 1, 4.0, "恢复生产"),
            "buyback_procedural_to_neutral": ("PROCEDURAL", 0, 0, 0.25, "回购前十股东"),
        }
        patch_counts = {}
        for name, mask in masks.items():
            category, stage, direction, hardness, keyword = specs[name]
            patch_counts[name] = int(mask.sum())
            ev.loc[mask, ["category", "stage", "direction", "hardness", "matched_keyword",
                          "classification_version"]] = [category, stage, direction, hardness,
                                                        keyword, VERSION]

        # Persist all corrected event dates and the small semantic patch set.
        for r in ev.itertuples(index=False):
            eff = None if pd.isna(r.effective_date) else pd.Timestamp(r.effective_date).date().isoformat()
            con.execute(
                """UPDATE announcement_event SET effective_date=?,category=?,stage=?,direction=?,hardness=?,
                   matched_keyword=?,classification_version=? WHERE canonical_code=? AND art_code=?""",
                (eff, r.category, int(r.stage), int(r.direction), float(r.hardness),
                 r.matched_keyword, r.classification_version, r.canonical_code, r.art_code),
            )
        con.commit()

        fixed = pd.read_sql_query(
            "SELECT * FROM announcement_event ORDER BY canonical_code,notice_date,art_code", con
        )
        features = ann.score_daily(fixed, market, membership)
        con.execute("DELETE FROM announcement_feature_daily WHERE source_id=?", (ann.SOURCE_ID,))
        if not features.empty:
            features.to_sql("announcement_feature_daily", con, if_exists="append", index=False)
        try:
            con.execute(
                "UPDATE source_registry SET notes=notes||? WHERE source_id=?",
                ("; rebuilt " + VERSION + " " + json.dumps(patch_counts, ensure_ascii=False), ann.SOURCE_ID),
            )
        except Exception:
            pass
        con.commit()

        fixed_eff = pd.to_datetime(fixed["effective_date"], errors="coerce")
        fixed_notice = pd.to_datetime(fixed["notice_date"], errors="coerce")
        gap = (fixed_eff - fixed_notice).dt.days
        changed_date = ~(
            old_eff.dt.normalize().fillna(pd.Timestamp("1900-01-01")) ==
            pd.to_datetime(ev["effective_date"]).dt.normalize().fillna(pd.Timestamp("1900-01-01"))
        )
        audit = {
            "version": VERSION,
            "events": int(len(ev)),
            "calendar_min": calendar.min().date().isoformat(),
            "calendar_max": calendar.max().date().isoformat(),
            "old_bad_gap_gt_bound": int((old_gap > args.max_gap_days).sum()),
            "changed_effective_date": int(changed_date.sum()),
            "corrected_effective_nonnull": int(fixed_eff.notna().sum()),
            "corrected_effective_null": int(fixed_eff.isna().sum()),
            "corrected_bad_gap_gt_bound": int((gap > args.max_gap_days).sum()),
            "corrected_same_or_before_notice": int((gap <= 0).sum()),
            "patch_counts": patch_counts,
            "semantic_patched_events": int(sum(patch_counts.values())),
            "feature_rows_rebuilt": int(len(features)),
            "feature_codes_rebuilt": int(features["code"].nunique()) if len(features) else 0,
            "scope": "title V1.1: timing correction plus four high-confidence semantic precedence corrections",
        }
        if args.audit_json:
            Path(args.audit_json).write_text(
                json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        if audit["corrected_bad_gap_gt_bound"] or audit["corrected_same_or_before_notice"]:
            raise SystemExit(2)
    finally:
        con.close()


if __name__ == "__main__":
    main()
