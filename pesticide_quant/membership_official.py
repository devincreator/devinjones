#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build PIT Shenwan pesticide membership from the official SW history file via CNEquity."""
import argparse, sqlite3
from datetime import datetime
from pathlib import Path
import pandas as pd

PESTICIDE_CODES={"220303","220803"}
SOURCE_ID="S013"

def canon_map(con):
    mp={}
    try:
        for canonical,historical in con.execute("SELECT canonical_code,historical_code FROM code_alias_history"):
            mp[str(historical).zfill(6)]=str(canonical).zfill(6)
    except sqlite3.Error:
        pass
    return mp

def fetch():
    from cnequity.adapters.sw.industry_history import fetch_sw_industry_intervals
    df=fetch_sw_industry_intervals().to_pandas()
    if df.empty: raise RuntimeError("official SW history returned no rows")
    df["source_code"]=df["symbol"].astype(str).str.split(".").str[0].str.zfill(6)
    df["start_date"]=pd.to_datetime(df["start_date"])
    df["industry_code"]=df["industry_code"].astype(str).str.replace(".0","",regex=False).str.strip()
    return df[["source_code","start_date","industry_code"]].copy()

def build(df, mp):
    df=df.copy()
    df["code"]=df["source_code"].map(lambda x: mp.get(x,x))
    df=df.sort_values(["code","start_date","industry_code"]).drop_duplicates(
        ["code","start_date","industry_code"],keep="last")
    intervals=[]
    for code,g in df.groupby("code",sort=True):
        g=g.sort_values("start_date")
        recs=g.to_dict("records")
        for i,r in enumerate(recs):
            intervals.append({
                "code":code,"source_code":r["source_code"],
                "industry_code":r["industry_code"],"in_date":r["start_date"],
                "end_exclusive":recs[i+1]["start_date"] if i+1<len(recs) else pd.NaT,
                "is_pesticide":r["industry_code"] in PESTICIDE_CODES,
            })
    alliv=pd.DataFrame(intervals)
    p=alliv[alliv["is_pesticide"]].copy()
    merged=[]
    for code,g in p.groupby("code",sort=True):
        cur=None
        for r in g.sort_values("in_date").to_dict("records"):
            if cur is None:
                cur=r.copy(); continue
            if pd.notna(cur["end_exclusive"]) and cur["end_exclusive"]==r["in_date"]:
                cur["end_exclusive"]=r["end_exclusive"]
                cur["industry_code"]=str(cur["industry_code"])+"->"+str(r["industry_code"])
            else:
                merged.append(cur); cur=r.copy()
        if cur is not None: merged.append(cur)
    return df,pd.DataFrame(merged)

def write(con, raw, members, audit_csv):
    now=datetime.now().isoformat(timespec="seconds")
    con.execute("DELETE FROM industry_membership_source_raw WHERE source_id=?",(SOURCE_ID,))
    con.execute("DELETE FROM industry_membership_history WHERE source_id=?",(SOURCE_ID,))
    raw_rows=[]
    for r in raw.to_dict("records"):
        raw_rows.append((r["source_code"],r["code"],r["start_date"].date().isoformat(),
                         r["industry_code"],"",None,SOURCE_ID,None,now))
    con.executemany("""INSERT INTO industry_membership_source_raw(
        source_code,canonical_code,start_date,industry_code,industry_name,update_time,source_id,raw_json,loaded_at
        ) VALUES(?,?,?,?,?,?,?,?,?)""",raw_rows)
    member_rows=[]
    for r in members.to_dict("records"):
        out_date=None
        if pd.notna(r["end_exclusive"]):
            out_date=(pd.Timestamp(r["end_exclusive"])-pd.Timedelta(days=1)).date().isoformat()
        version="SW2021" if "220803" in str(r["industry_code"]) else "SW_LEGACY"
        member_rows.append((r["code"],r["source_code"],"农药","850333",r["industry_code"],"农药",
                            version,r["in_date"].date().isoformat(),out_date,None,SOURCE_ID,now))
    con.executemany("""INSERT INTO industry_membership_history(
      code,source_code,sw_name,sw_index_code,source_industry_code,source_industry_name,
      classification_version,in_date,out_date,source_update_time,source_id,loaded_at
      ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",member_rows)

    asof="2026-01-21"
    expected={r[0]:r[1] for r in con.execute("SELECT code,name FROM company_master WHERE current_member=1")}
    active={r[0] for r in con.execute("""SELECT DISTINCT code FROM industry_membership_history
        WHERE in_date<=? AND (out_date IS NULL OR out_date>=?)""",(asof,asof))}
    audit=[]
    for code,name in sorted(expected.items()):
        audit.append({"code":code,"name":name,"expected_current":1,"active_official":int(code in active),
                      "status":"MATCH" if code in active else "MISSING_OFFICIAL"})
    for code in sorted(active-set(expected)):
        audit.append({"code":code,"name":"","expected_current":0,"active_official":1,"status":"EXTRA_OFFICIAL"})
    Path(audit_csv).parent.mkdir(parents=True,exist_ok=True)
    pd.DataFrame(audit).to_csv(audit_csv,index=False,encoding="utf-8-sig")
    missing=sorted(set(expected)-active)
    status="LOADED" if not missing else "AUDIT_FAILED"
    con.execute("""UPDATE ingestion_job SET source_id=?,status=?,target_entities=?,loaded_entities=?,
       loaded_rows=?,min_date=?,max_date=?,last_attempt_ts=?,note=? WHERE job_id='J003'""",
       (SOURCE_ID,status,len(expected),len(set(members["code"])) if len(members) else 0,len(members),
        members["in_date"].min().date().isoformat() if len(members) else None,
        (members["end_exclusive"].dropna().max()-pd.Timedelta(days=1)).date().isoformat()
          if len(members) and members["end_exclusive"].notna().any() else None,
        now,f"official SW history; missing current33={missing}"))
    con.execute("""INSERT OR REPLACE INTO source_registry(
       source_id,data_layer,source_name,url,coverage,source_type,reliability,v1_status,notes)
       VALUES(?,?,?,?,?,?,?,?,?)""",(SOURCE_ID,"industry_membership_history",
       "SW official StockClassifyUse_stock.xls via CNEquity",
       "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls",
       "all-stock SW classification history","OFFICIAL_XLS","HIGH",status,
       "PIT intervals derived before pesticide filtering; 220303/220803"))
    con.commit()
    return status,missing,active

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",required=True)
    ap.add_argument("--audit-csv",default="work/membership_audit.csv")
    ap.add_argument("--export-csv",default="work/pesticide_membership_history.csv")
    args=ap.parse_args()
    con=sqlite3.connect(args.db)
    try:
        df=fetch()
        raw,members=build(df,canon_map(con))
        if members.empty: raise RuntimeError("no pesticide intervals found for 220303/220803")
        status,missing,active=write(con,raw,members,args.audit_csv)
        exp=members.copy()
        exp["in_date"]=exp["in_date"].dt.date.astype(str)
        exp["out_date"]=exp["end_exclusive"].map(
            lambda x:"" if pd.isna(x) else (pd.Timestamp(x)-pd.Timedelta(days=1)).date().isoformat())
        exp.to_csv(args.export_csv,index=False,encoding="utf-8-sig")
        print({"official_rows":len(df),"membership_intervals":len(members),
               "historical_codes":int(members["code"].nunique()),"active_asof":len(active),
               "status":status,"missing_current33":missing})
        if status!="LOADED":
            raise SystemExit(2)
    finally:
        con.close()

if __name__=="__main__":
    main()
