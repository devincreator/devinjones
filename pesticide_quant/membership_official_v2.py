#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Official Shenwan membership loader v2 with frozen official fallback.

Primary source: live official SW StockClassifyUse_stock.xls via CNEquity.
Fallback source: the 47 pesticide intervals frozen from a successful official
fetch on 2026-08-20. The fallback is not a web-validation reconstruction; it is
an exact export of the official-source production result.

The single 002004 current-provider snapshot mismatch remains explicit and does
not override the official PIT history.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

import membership_official as base

ALLOWED_CURRENT_SNAPSHOT_DIFF = {"002004"}
STATUS_LIVE = "LOADED_OFFICIAL_WITH_SNAPSHOT_DIFF"
STATUS_FROZEN = "LOADED_OFFICIAL_FROZEN_FALLBACK"
FROZEN = Path(__file__).resolve().parent / "data" / "pesticide_membership_official_frozen_20260820.csv"


def load_frozen():
    if not FROZEN.exists():
        raise FileNotFoundError(FROZEN)
    x = pd.read_csv(FROZEN, dtype={"code":str,"source_code":str,"industry_code":str})
    required={"code","source_code","industry_code","in_date","end_exclusive","is_pesticide"}
    missing=required-set(x.columns)
    if missing:
        raise RuntimeError(f"frozen membership missing columns: {sorted(missing)}")
    x["code"]=x["code"].astype(str).str.zfill(6)
    x["source_code"]=x["source_code"].astype(str).str.zfill(6)
    x["in_date"]=pd.to_datetime(x["in_date"])
    x["end_exclusive"]=pd.to_datetime(x["end_exclusive"],errors="coerce")
    x["is_pesticide"]=True
    if len(x)!=47 or x["code"].nunique()!=46:
        raise RuntimeError(f"frozen official membership invariant failed rows={len(x)} codes={x['code'].nunique()}")
    # base.write expects a raw-like table. For fallback, preserve the exact
    # pesticide interval starts as source rows; provenance explicitly says this
    # is a frozen official subset rather than the full 12,897-row raw workbook.
    raw=x[["source_code","code","in_date","industry_code"]].rename(columns={"in_date":"start_date"}).copy()
    members=x[["code","source_code","industry_code","in_date","end_exclusive","is_pesticide"]].copy()
    return raw,members


def normalize_status(con,status,missing_set,source_mode):
    if missing_set and not missing_set.issubset(ALLOWED_CURRENT_SNAPSHOT_DIFF):
        print({"status":"AUDIT_FAILED","unexpected_missing_current":sorted(missing_set)})
        raise SystemExit(2)
    if source_mode=="LIVE":
        final=STATUS_LIVE if missing_set else "LOADED"
        note=("live official SW PIT history loaded; "
              f"current provider snapshot differs for {sorted(missing_set)}; official history remains production universe")
    else:
        final=STATUS_FROZEN
        note=("frozen official SW PIT history from successful 2026-08-20 official fetch; "
              f"47 intervals/46 codes; current provider snapshot differs for {sorted(missing_set)}; "
              "used only because live SW host was unavailable")
    con.execute("UPDATE ingestion_job SET status=?,note=? WHERE job_id='J003'",(final,note))
    con.execute("UPDATE source_registry SET v1_status=?,notes=? WHERE source_id='S013'",(final,note))
    con.commit()
    return final


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",required=True)
    ap.add_argument("--audit-csv",default="work/membership_audit.csv")
    ap.add_argument("--export-csv",default="work/pesticide_membership_history.csv")
    args=ap.parse_args()

    con=sqlite3.connect(args.db)
    try:
        source_mode="LIVE"
        live_rows=None
        try:
            df=base.fetch()
            live_rows=len(df)
            raw,members=base.build(df,base.canon_map(con))
            if members.empty:
                raise RuntimeError("live official source produced no pesticide intervals")
            print("MEMBERSHIP_SOURCE LIVE_OFFICIAL",live_rows,len(members),members["code"].nunique())
        except Exception as e:
            source_mode="FROZEN"
            print("WARN live official SW fetch failed; using verified frozen official snapshot:",repr(e))
            raw,members=load_frozen()
            print("MEMBERSHIP_SOURCE FROZEN_OFFICIAL_20260820",len(members),members["code"].nunique())

        status,missing,active=base.write(con,raw,members,args.audit_csv)
        missing_set=set(missing)
        final_status=normalize_status(con,status,missing_set,source_mode)

        exp=members.copy()
        exp["in_date"]=pd.to_datetime(exp["in_date"]).dt.date.astype(str)
        exp["out_date"]=exp["end_exclusive"].map(
            lambda x:"" if pd.isna(x) else (pd.Timestamp(x)-pd.Timedelta(days=1)).date().isoformat())
        Path(args.export_csv).parent.mkdir(parents=True,exist_ok=True)
        exp.to_csv(args.export_csv,index=False,encoding="utf-8-sig")

        print({
            "source_mode":source_mode,
            "official_raw_rows_live":live_rows,
            "membership_intervals":len(members),
            "historical_codes":int(members["code"].nunique()),
            "active_asof":len(active),
            "status":final_status,
            "current_snapshot_diff":sorted(missing_set),
            "frozen_file":str(FROZEN) if source_mode=="FROZEN" else None,
        })
    finally:
        con.close()


if __name__=="__main__":main()
