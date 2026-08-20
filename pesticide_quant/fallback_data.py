#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse,csv,datetime as dt,io,sqlite3
from pathlib import Path
import pandas as pd, requests

ASTOCK_SOURCE="https://github.com/newbiestring-lang/astock"
FIN_RAW="https://raw.githubusercontent.com/songjian/update-company-financial-data-from-eastmoney.com/main/lrb/{pfx}{code}.csv"

def universe(path):
    with open(path,encoding="utf-8-sig",newline="") as f: rows=list(csv.DictReader(f))
    return {r["code"].strip():r for r in rows if r.get("code","").strip()}

def ex(code,r):
    x=(r.get("exchange") or "").upper()
    if x:return x
    if code.startswith(("60","68")):return "SH"
    if code.startswith(("92","43","83","87")):return "BJ"
    return "SZ"

def market(db,uni_path,data_dir):
    uni=universe(uni_path);root=Path(data_dir);files=sorted(root.glob("kline_*.parquet"))
    if not files:raise RuntimeError("no astock parquet files")
    frames=[];targets=sorted(uni)
    for p in files:
        try:z=pd.read_parquet(p,filters=[("code","in",targets)])
        except Exception:
            z=pd.read_parquet(p);z=z[z["code"].astype(str).str.zfill(6).isin(targets)]
        if len(z):frames.append(z)
    if not frames:raise RuntimeError("astock has no target rows")
    x=pd.concat(frames,ignore_index=True);x["code"]=x["code"].astype(str).str.zfill(6);x["date"]=pd.to_datetime(x["date"])
    con=sqlite3.connect(db);now=dt.datetime.now().isoformat(timespec="seconds");n=0
    for r in x.itertuples(index=False):
        vals=(r.date.date().isoformat(),r.code,None,None,None,None,None,
              None if pd.isna(r.pctChg) else float(r.pctChg),None if pd.isna(r.volume) else float(r.volume),
              None if pd.isna(r.amount) else float(r.amount),None if pd.isna(r.turn) else float(r.turn),
              None,None if pd.isna(r.close) else float(r.close),1 if pd.notna(r.close) else 0,None,None,"S014",now,
              None if pd.isna(r.open) else float(r.open),None if pd.isna(r.high) else float(r.high),
              None if pd.isna(r.low) else float(r.low),None,None)
        con.execute("""INSERT INTO market_daily(
          trade_date,code,open_raw,high_raw,low_raw,close_raw,prev_close_raw,pct_chg_pct,volume_shares,amount_cny,
          turnover_pct,adj_factor,close_qfq,is_trade,is_st,limit_status,source_id,ingest_ts,open_qfq,high_qfq,low_qfq,
          amplitude_pct,change_amt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(trade_date,code) DO UPDATE SET
          pct_chg_pct=COALESCE(excluded.pct_chg_pct,market_daily.pct_chg_pct),volume_shares=COALESCE(excluded.volume_shares,market_daily.volume_shares),
          amount_cny=COALESCE(excluded.amount_cny,market_daily.amount_cny),turnover_pct=COALESCE(excluded.turnover_pct,market_daily.turnover_pct),
          close_qfq=COALESCE(excluded.close_qfq,market_daily.close_qfq),open_qfq=COALESCE(excluded.open_qfq,market_daily.open_qfq),
          high_qfq=COALESCE(excluded.high_qfq,market_daily.high_qfq),low_qfq=COALESCE(excluded.low_qfq,market_daily.low_qfq),ingest_ts=excluded.ingest_ts""",vals);n+=1
    loaded=con.execute("SELECT COUNT(DISTINCT code) FROM market_daily WHERE close_qfq IS NOT NULL").fetchone()[0];mn,mx=con.execute("SELECT MIN(trade_date),MAX(trade_date) FROM market_daily").fetchone()
    con.execute("""UPDATE ingestion_job SET status='FALLBACK_ASTOCK',loaded_entities=?,loaded_rows=?,min_date=?,max_date=?,last_attempt_ts=?,note=? WHERE job_id='J001'""",(loaded,n,mn,mx,now,"AStock BaoStock qfq fallback; period/coverage must be audited"))
    con.execute("""INSERT OR REPLACE INTO source_registry VALUES(?,?,?,?,?,?,?,?,?)""",("S014","market_daily","AStock BaoStock qfq",ASTOCK_SOURCE,"2020-06-01~2026-04-03 approx","GITHUB_PARQUET","MEDIUM","FALLBACK","README warns some early histories are missing; BSE absent"));con.commit();con.close();print({"rows":n,"loaded_codes":loaded,"min":mn,"max":mx})

def pdate(x):
    if pd.isna(x) or not x:return None
    return str(x)[:10]

def finance(db,uni_path):
    uni=universe(uni_path);s=requests.Session();s.headers["User-Agent"]="Mozilla/5.0 pesticide-quant-github-actions";con=sqlite3.connect(db);now=dt.datetime.now().isoformat(timespec="seconds");rows_n=0;codes_n=0
    for code,r in sorted(uni.items()):
        market_ex=ex(code,r);candidates=[(market_ex,code)]
        if code=="920819":candidates += [("BJ","833819"),("SZ","833819")]
        if code=="920866":candidates += [("BJ","870866"),("SZ","870866")]
        df=None
        for pfx,c in candidates:
            url=FIN_RAW.format(pfx=pfx,code=c);resp=s.get(url,timeout=30)
            if resp.status_code==200 and len(resp.content)>100:
                try:df=pd.read_csv(io.BytesIO(resp.content),dtype={"SECURITY_CODE":str});break
                except Exception:pass
        if df is None or df.empty:print("FIN_FALLBACK_MISS",code);continue
        local=0
        for _,d in df.iterrows():
            rp=pdate(d.get("REPORT_DATE"));ann=pdate(d.get("NOTICE_DATE"));upd=pdate(d.get("UPDATE_DATE"));av=max([x for x in (ann,upd) if x],default=None)
            if not rp or not av:continue
            def num(k):
                v=pd.to_numeric(d.get(k),errors="coerce");return None if pd.isna(v) else float(v)
            rev=upd or ann or "0"
            con.execute("""INSERT INTO financial_quarterly(
             code,report_period,report_type,ann_date,available_date,revision_id,revenue_cny,revenue_yoy_pct,
             net_profit_parent_cny,net_profit_yoy_pct,deduct_np_cny,roe_weighted_pct,gross_margin_pct,
             net_margin_pct,cfo_cny,total_assets_cny,total_liab_cny,equity_parent_cny,inventory_cny,ar_cny,capex_cny,
             source_id,ingest_ts,update_date,source_asof) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(code,report_period,ann_date,revision_id) DO UPDATE SET
             available_date=excluded.available_date,revenue_cny=excluded.revenue_cny,revenue_yoy_pct=excluded.revenue_yoy_pct,
             net_profit_parent_cny=excluded.net_profit_parent_cny,net_profit_yoy_pct=excluded.net_profit_yoy_pct,
             deduct_np_cny=excluded.deduct_np_cny,source_id=excluded.source_id,ingest_ts=excluded.ingest_ts,
             update_date=excluded.update_date,source_asof=excluded.source_asof""",
             (code,rp,str(d.get("REPORT_TYPE") or ""),ann or av,av,rev,num("TOTAL_OPERATE_INCOME"),num("TOTAL_OPERATE_INCOME_YOY"),
              num("PARENT_NETPROFIT"),num("PARENT_NETPROFIT_YOY"),num("DEDUCT_PARENT_NETPROFIT"),None,None,None,None,None,None,None,None,None,None,
              "S015",now,upd,dt.date.today().isoformat()));local+=1;rows_n+=1
        if local:codes_n+=1
        con.commit();print("FIN_FALLBACK",code,local)
    mn,mx=con.execute("SELECT MIN(available_date),MAX(available_date) FROM financial_quarterly").fetchone()
    con.execute("""UPDATE ingestion_job SET status='FALLBACK_GITHUB_ARCHIVE',loaded_entities=?,loaded_rows=?,min_date=?,max_date=?,last_attempt_ts=?,note=? WHERE job_id='J002'""",(codes_n,rows_n,mn,mx,now,"Eastmoney historical CSV archive; conservative PIT max(NOTICE_DATE,UPDATE_DATE)"))
    con.execute("""INSERT OR REPLACE INTO source_registry VALUES(?,?,?,?,?,?,?,?,?)""",("S015","financial_quarterly","Eastmoney CSV archive","https://github.com/songjian/update-company-financial-data-from-eastmoney.com","repository history","GITHUB_CSV","MEDIUM","FALLBACK","available_date=max(NOTICE_DATE,UPDATE_DATE); not full revision history"));con.commit();con.close();print({"rows":rows_n,"loaded_codes":codes_n,"min_available":mn,"max_available":mx})

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--db",required=True);ap.add_argument("--universe-csv",required=True);ap.add_argument("--mode",choices=["market","finance"],required=True);ap.add_argument("--data-dir",default="");a=ap.parse_args()
    if a.mode=="market":market(a.db,a.universe_csv,a.data_dir)
    else:finance(a.db,a.universe_csv)
if __name__=="__main__":main()
