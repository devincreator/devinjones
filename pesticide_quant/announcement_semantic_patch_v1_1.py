#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Conservative semantic precedence patch for ANN_CHAIN_TITLE_V1.

This is intentionally small.  It fixes only cases where an earlier generic
keyword rule demonstrably overrides a more specific disclosure meaning:

* inquiry reply was being caught as a negative inquiry;
* pledge release was being caught as a new pledge;
* production recovery could be caught as a shutdown;
* procedural top-shareholder notices around a buyback were treated as an
  economic buyback event.

No new positive category is invented.  After patching the event table, daily
announcement features are rebuilt with the original scorer.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pandas as pd

import announcement_intelligence as ann

VERSION = "ANN_CHAIN_TITLE_V1_1_SEMANTIC"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
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

        patches = []

        def apply(mask, category, stage, direction, hardness, keyword, reason):
            rows = ev.loc[mask, ["canonical_code", "art_code"]]
            for r in rows.itertuples(index=False):
                patches.append((category, stage, direction, hardness, keyword, VERSION,
                                r.canonical_code, r.art_code, reason))
            ev.loc[mask, ["category", "stage", "direction", "hardness", "matched_keyword",
                          "classification_version"]] = [category, stage, direction, hardness,
                                                        keyword, VERSION]
            return int(mask.sum())

        inquiry_reply = ev["category"].eq("INQUIRY") & ev["title"].str.contains(
            r"回复问询函|问询函回复|问询函的回复|问询函的回函|回复关注函|关注函回复|监管工作函回复",
            regex=True, na=False,
        )
        pledge_recovery = ev["category"].eq("PLEDGE") & ev["title"].str.contains(
            r"解除股份质押|解除质押", regex=True, na=False,
        )
        risk_recovery = ev["category"].eq("RISK") & ev["title"].str.contains(
            r"复产|恢复生产|解除停产|整改完成", regex=True, na=False,
        )
        buyback_procedural = ev["category"].eq("BUYBACK") & ev["title"].str.contains(
            r"回购股份事项前十大股东|回购股份事项前十名股东|前十大无限售|前十名无限售",
            regex=True, na=False,
        )

        counts = {
            "inquiry_reply_negative_to_neutral": apply(
                inquiry_reply, "INQUIRY_REPLY", 2, 0, 1.0, "回复函",
                "specific reply semantics override generic inquiry keyword",
            ),
            "pledge_to_release": apply(
                pledge_recovery, "PLEDGE_RECOVERY", 5, 1, 2.0, "解除质押",
                "pledge release must override generic pledge keyword",
            ),
            "shutdown_to_recovery": apply(
                risk_recovery, "RISK_RECOVERY", 7, 1, 4.0, "恢复生产",
                "recovery semantics must override shutdown keyword",
            ),
            "buyback_procedural_to_neutral": apply(
                buyback_procedural, "PROCEDURAL", 0, 0, 0.25, "回购前十股东",
                "top-shareholder disclosure is procedural, not buyback execution",
            ),
        }

        # Persist only the conservative semantic changes.  family_key/novelty are
        # deliberately left untouched in V1.1 so this patch isolates direction
        # semantics rather than redesigning event-chain similarity at the same time.
        for category, stage, direction, hardness, keyword, version, code, art, _ in patches:
            con.execute(
                """UPDATE announcement_event SET category=?,stage=?,direction=?,hardness=?,
                   matched_keyword=?,classification_version=?
                   WHERE canonical_code=? AND art_code=?""",
                (category, stage, direction, hardness, keyword, version, code, art),
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
                ("; semantic patch " + VERSION + " " + json.dumps(counts, ensure_ascii=False), ann.SOURCE_ID),
            )
        except Exception:
            pass
        con.commit()

        eff = pd.to_datetime(fixed["effective_date"], errors="coerce")
        notice = pd.to_datetime(fixed["notice_date"], errors="coerce")
        gap = (eff - notice).dt.days
        audit = {
            "version": VERSION,
            "patch_counts": counts,
            "patched_events": int(sum(counts.values())),
            "event_rows": int(len(fixed)),
            "feature_rows_rebuilt": int(len(features)),
            "feature_codes_rebuilt": int(features["code"].nunique()) if len(features) else 0,
            "pit_bad_gap_gt_15_after_patch": int((gap > 15).sum()),
            "pit_same_or_before_notice_after_patch": int((gap <= 0).sum()),
            "scope": "minimal precedence corrections only; no full-text inference",
        }
        if args.audit_json:
            Path(args.audit_json).write_text(
                json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        if audit["pit_bad_gap_gt_15_after_patch"] or audit["pit_same_or_before_notice_after_patch"]:
            raise SystemExit(2)
    finally:
        con.close()


if __name__ == "__main__":
    main()
