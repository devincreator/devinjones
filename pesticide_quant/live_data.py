#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse, csv, datetime as dt, sqlite3, time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

KLINE="https://push2his.eastmoney.com/api/qt/stock/kline/get"
FIN="https://datacenter-web.eastmoney.com/api/data/v1/get"
ALIASES={
 "920819":[("833819","2020-07-27","2025-05-05"),("920819","2025-05-06",None)],
 "920866":[("870866","2022-12-09","2025-10-08"),("920866","2025-10-09",None)],
}

def load_uni(path):
    with open(path,encoding="utf-8-sig",newline="") as f: rows=list(csv.DictReader(f))
    out=[]; seen=set()
    for r in rows:
        c=r["code"].strip()
        if not c or c in seen: continue
        ex=(r.get("exchange") or "").upper()
        if not ex:
            ex="SH" if c.startswith(("60","68")) else ("BJ" if c.startswith(("92","43","83","87")) else "SZ")
        out.append((c,ex,r.get("name",""))); seen.add(c)
    return out

def sess():
    s=requests.Session()
    retry=Retry(total=4,connect=4,read=4,backoff_factor=.7,
      status_forcelist=(429,500,502,503,504),allowed_methods=frozenset(["GET"]))
    s.mount("https://",HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent":"Mozilla/5.0 pesticide-quant-github-actions",
                      "Referer":"https://quote.eastmoney.com/","Accept":"application/json,*/*"})
    return s

def aliases(code): return ALIASES.get(code,[(code,None,None)])
def market_id(ex): return 1 if ex=="SH" else 0

def fetch_kline(s, code, ex, fqt):
    r=s.get(KLINE,params={"secid":f"{market_id(ex)}.{code}","fields1":"f1,f2,f3,f4,f5,f6",
        "fields2":"f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt":101,"fqt":fqt,"beg":"0","end":"20500000","lmt":1000000},timeout=30)
    r.raise_for_status(); obj=r.json(); lines=((obj.get("data") or {}).get("klines") or [])
    out=[]
    for line in lines:
        p=line.split(",")
        if len(p)<11: continue
        out.append({"date":p[0],"open":float(p[1]),"close":float(p[2]),"high":float(p[3]),
                    "low":float(p[4]),"volume":float(p[5]),"amount":float(p[6]),
                    "amplitude":float(p[7]),"pct":float(p[8]),"change":float(p[9]),"turnover":float(p[10])})
    return out

def merged_kline(s, canonical, ex, fqt):
    by={}
    for a,start,end in aliases(canonical):
        try: rows=fetch_kline(s,a,ex,fqt)
        except Exception as e:
            print("WARN kline",canonical,a,fqt,repr(e)); continue
        for r in rows:
            if start and r["date"]<start: continue
            if end and r["date"]>end: continue
            by[r["date"]]=r
    return [by[k] for k in sorted(by)]

def upsert_market(con, code, raw, qfq):
    q={r["date"]:r for r in qfq}; prev=None; now=dt.datetime.now().isoformat(timespec="seconds"); n=0
    for r in sorted(raw,key=lambda x:x["date"]):
        z=q.get(r["date"])
        con.execute("""INSERT INTO market_daily(
          trade_date,code,open_raw,high_raw,low_raw,close_raw,prev_close_raw,pct_chg_pct,
          volume_shares,amount_cny,turnover_pct,adj_factor,close_qfq,is_trade,is_st,limit_status,
          source_id,ingest_ts,open_qfq,high_qfq,low_qfq,amplitude_pct,change_amt)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(trade_date,code) DO UPDATE SET
          open_raw=excluded.open_raw,high_raw=excluded.high_raw,low_raw=excluded.low_raw,
          close_raw=excluded.close_raw,prev_close_raw=excluded.prev_close_raw,pct_chg_pct=excluded.pct_chg_pct,
          volume_shares=excluded.volume_shares,amount_cny=excluded.amount_cny,turnover_pct=excluded.turnover_pct,
          close_qfq=excluded.close_qfq,open_qfq=excluded.open_qfq,high_qfq=excluded.high_qfq,low_qfq=excluded.low_qfq,
          amplitude_pct=excluded.amplitude_pct,change_amt=excluded.change_amt,source_id=excluded.source_id,ingest_ts=excluded.ingest_ts""",
          (r["date"],code,r["open"],r["high"],r["low"],r["close"],prev,r["pct"],r["volume"],r["amount"],r["turnover"],
           None,z["close"] if z else None,1,None,None,"S009",now,
           z["open"] if z else None,z["high"] if z else None,z["low"] if z else None,r["amplitude"],r["change"]))
        prev=r["close"]; n+=1
    return n

def pdate(x):
    if not x: return None
    s=str(x)[:10]
    try: dt.datetime.strptime(s,"%Y-%m-%d"); return s
    except: return None

def fnum(d,*keys):
    for k in keys:
        v=d.get(k)
        if v not in (None,"","-"):
            try:return float(v)
            except:pass
    return None

def fetch_fin(s,code):
    out=[]; page=1
    while True:
        r=s.get(FIN,params={"sortColumns":"REPORTDATE","sortTypes":"-1","pageSize":100,"pageNumber":page,
          "reportName":"RPT_LICO_FN_CPD","columns":"ALL","filter":f'(SECURITY_CODE="{code}")',
          "source":"WEB","client":"WEB"},timeout=30)
        r.raise_for_status(); result=(r.json().get("result") or {}); rows=result.get("data") or []; out.extend(rows)
        if page>=int(result.get("pages") or 1) or not rows: break
        page+=1
    return out

def upsert_fin(con,code,rows):
    now=dt.datetime.now().isoformat(timespec="seconds"); seen=set(); n=0
    for d in rows:
        rp=pdate(d.get("REPORTDATE")); ann=pdate(d.get("NOTICE_DATE")); upd=pdate(d.get("UPDATE_DATE"))
        av=max([x for x in (ann,upd) if x],default=None)
        if not rp or not av: continue
        rev=upd or ann or "0"; key=(rp,rev)
        if key in seen: continue
        seen.add(key)
        con.execute("""INSERT INTO financial_quarterly(
          code,report_period,report_type,ann_date,available_date,revision_id,revenue_cny,revenue_yoy_pct,
          net_profit_parent_cny,net_profit_yoy_pct,deduct_np_cny,roe_weighted_pct,gross_margin_pct,
          net_margin_pct,cfo_cny,total_assets_cny,total_liab_cny,equity_parent_cny,inventory_cny,ar_cny,capex_cny,
          source_id,ingest_ts,update_date,source_asof)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(code,report_period,ann_date,revision_id) DO UPDATE SET
          available_date=excluded.available_date,revenue_cny=excluded.revenue_cny,
          revenue_yoy_pct=excluded.revenue_yoy_pct,net_profit_parent_cny=excluded.net_profit_parent_cny,
          net_profit_yoy_pct=excluded.net_profit_yoy_pct,deduct_np_cny=excluded.deduct_np_cny,
          roe_weighted_pct=excluded.roe_weighted_pct,gross_margin_pct=excluded.gross_margin_pct,
          source_id=excluded.source_id,ingest_ts=excluded.ingest_ts,update_date=excluded.update_date,source_asof=excluded.source_asof""",
          (code,rp,d.get("DATATYPE") or d.get("QDATE") or "",ann or av,av,rev,
           fnum(d,"TOTAL_OPERATE_INCOME"),fnum(d,"YSTZ","TOTAL_OPERATE_INCOME_YOY"),
           fnum(d,"PARENT_NETPROFIT"),fnum(d,"SJLTZ","PARENT_NETPROFIT_YOY"),fnum(d,"DEDUCT_PARENT_NETPROFIT"),
           fnum(d,"WEIGHTAVG_ROE"),fnum(d,"XSMLL"),None,None,None,None,None,None,None,None,
           "S010",now,upd,dt.date.today().isoformat()))
        n+=1
    return n

def set_job(con,j,status,entities,rows):
    table="market_daily" if j=="J001" else "financial_quarterly"
    datecol="trade_date" if j=="J001" else "report_period"
    mn,mx=con.execute(f"SELECT MIN({datecol}),MAX({datecol}) FROM {table}").fetchone()
    con.execute("""UPDATE ingestion_job SET status=?,loaded_entities=?,loaded_rows=?,min_date=?,max_date=?,
       last_attempt_ts=?,note=? WHERE job_id=?""",(status,entities,rows,mn,mx,
       dt.datetime.now().isoformat(timespec="seconds"),f"github actions {status}",j))
    con.commit()

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--db",required=True); ap.add_argument("--universe-csv",required=True)
    ap.add_argument("--market-only",action="store_true"); ap.add_argument("--finance-only",action="store_true")
    args=ap.parse_args(); uni=load_uni(args.universe_csv); con=sqlite3.connect(args.db); s=sess()
    con.execute("UPDATE ingestion_job SET target_entities=? WHERE job_id IN ('J001','J002')",(len(uni),)); con.commit()
    if not args.finance_only:
        e=rr=0
        for i,(c,ex,name) in enumerate(uni,1):
            try:
                raw=merged_kline(s,c,ex,0); q=merged_kline(s,c,ex,1); n=upsert_market(con,c,raw,q); con.commit()
                print("MARKET",i,len(uni),c,n)
                if n:e+=1;rr+=n
            except Exception as x: print("MARKET_ERR",c,repr(x))
            time.sleep(.05)
        set_job(con,"J001","FETCHED" if e else "FAILED",e,rr)
        if not e: raise SystemExit(2)
    if not args.market_only:
        e=rr=0
        for i,(c,ex,name) in enumerate(uni,1):
            rows=[]
            for a,_,__ in [(c,None,None)]+[(x[0],None,None) for x in aliases(c) if x[0]!=c]:
                try: rows=fetch_fin(s,a)
                except Exception as x: print("FIN_ERR",c,a,repr(x)); rows=[]
                if rows: break
            n=upsert_fin(con,c,rows); con.commit(); print("FIN",i,len(uni),c,n)
            if n:e+=1;rr+=n
            time.sleep(.05)
        set_job(con,"J002","FETCHED" if e else "FAILED",e,rr)
        if not e: raise SystemExit(2)
    con.close()

if __name__=="__main__": main()
