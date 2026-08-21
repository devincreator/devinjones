#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Official Shenwan membership loader v2.

Uses the existing official loader, but treats the single documented 002004
current-snapshot mismatch as a warning rather than a failure of the official
PIT history itself.
"""
import argparse
import sqlite3
from pathlib import Path
import pandas as pd

import membership_official as base

ALLOWED_CURRENT_SNAPSHOT_DIFF = {"002004"}
STATUS_OK = "LOADED_OFFICIAL_WITH_SNAPSHOT_DIFF"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--audit-csv", default="work/membership_audit.csv")
    ap.add_argument("--export-csv", default="work/pesticide_membership_history.csv")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    try:
        df = base.fetch()
        raw, members = base.build(df, base.canon_map(con))
        if members.empty:
            raise RuntimeError("no pesticide intervals found for 220303/220803")

        status, missing, active = base.write(con, raw, members, args.audit_csv)
        missing_set = set(missing)

        if missing_set and missing_set.issubset(ALLOWED_CURRENT_SNAPSHOT_DIFF):
            status = STATUS_OK
            note = (
                "official SW PIT history loaded; current provider snapshot differs for "
                f"{sorted(missing_set)}; official SW history remains production universe"
            )
            con.execute(
                "UPDATE ingestion_job SET status=?,note=? WHERE job_id='J003'",
                (status, note),
            )
            con.execute(
                "UPDATE source_registry SET v1_status=?,notes=? WHERE source_id='S013'",
                (status, note),
            )
            con.commit()
        elif missing_set:
            print({"status": "AUDIT_FAILED", "unexpected_missing_current": sorted(missing_set)})
            raise SystemExit(2)

        exp = members.copy()
        exp["in_date"] = exp["in_date"].dt.date.astype(str)
        exp["out_date"] = exp["end_exclusive"].map(
            lambda x: "" if pd.isna(x) else (pd.Timestamp(x) - pd.Timedelta(days=1)).date().isoformat()
        )
        Path(args.export_csv).parent.mkdir(parents=True, exist_ok=True)
        exp.to_csv(args.export_csv, index=False, encoding="utf-8-sig")

        print({
            "official_rows": len(df),
            "membership_intervals": len(members),
            "historical_codes": int(members["code"].nunique()),
            "active_asof": len(active),
            "status": status,
            "current_snapshot_diff": sorted(missing_set),
        })
    finally:
        con.close()


if __name__ == "__main__":
    main()
