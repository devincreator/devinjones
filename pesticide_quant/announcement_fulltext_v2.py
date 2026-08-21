#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Full-text PIT announcement event-chain features for the pesticide 60d model.

Design principles
-----------------
* Full text comes from Eastmoney's public announcement content endpoint by art_code.
* An announcement can only affect features on its already-audited effective_date.
* Full text is used to detect milestone progression, project progress, commercial
  realization and negative execution changes; it is NOT reduced to sentiment.
* Repeated disclosures are down-weighted by topic and content signature.
* Same-topic change is explicit: stage_delta/progress_delta versus the previous
  disclosure in that company-topic chain.
* Large periodic reports / routine governance documents are excluded from the
  full-text fetch queue because they duplicate the PIT financial layer and create
  boilerplate false positives.

This is V2 research code. It preserves title V1.1 as a separate baseline.
"""
from __future__ import annotations
import argparse, concurrent.futures as cf, datetime as dt, hashlib, html, json, math, re, sqlite3, unicodedata
from pathlib import Path
import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CONTENT_URL="https://np-cnotice-stock.eastmoney.com/api/content/ann"
SOURCE_ID="S021"; VERSION="ANN_CHAIN_FULLTEXT_V2"
SKIP_TITLE=("年度报告","半年度报告","季度报告","年度审计报告","内部控制审计报告","法律意见书","股东大会决议","董事会会议决议","监事会会议决议","独立董事","审计委员会","募集资金存放与使用","社会责任报告","ESG报告")
CANDIDATE_HINTS=("项目","投资","建设","投产","试生产","达产","扩产","产能","许可","批复","环评","登记证","合同","订单","中标","客户","业绩","预告","修正","扭亏","停产","复产","延期","终止","事故","处罚","立案","诉讼","仲裁","整改","回购","增持","减持","质押","解除质押","问询","回复","重大事项")
POS_RULES=[
 (9,4.0,"COMMERCIAL",["重大合同","合同金额","订单金额","获得订单","中标金额","签订合同","签署合同"]),
 (9,4.0,"PROJECT",["正式投产","投入生产","竣工投产","实现达产","已达产","全面达产"]),
 (8,4.0,"PROJECT",["试生产","试运行","进入试生产","开始试生产"]),
 (7,3.5,"PROJECT",["竣工验收","建设完成","安装完成","项目竣工","工程完工"]),
 (5,3.0,"PROJECT",["开工建设","项目开工","建设进度","工程进度","设备安装"]),
 (4,3.0,"PROJECT",["环评批复","取得批复","取得许可","取得备案","生产许可证","农药登记证","安全生产许可证"]),
 (2,2.0,"PROJECT",["签署投资协议","投资建设","项目投资","增资扩产","扩建项目"]),
 (1,1.0,"PROJECT",["拟投资","投资计划","规划建设","项目规划"]),
 (9,4.0,"EARNINGS",["业绩预增","大幅预增","扭亏为盈","上调业绩","上修业绩"]),
 (7,3.0,"RECOVERY",["复产","恢复生产","解除停产","整改完成","恢复正常生产"]),]
NEG_RULES=[
 (0,4.0,"REGULATORY",["立案调查","行政处罚","处罚决定","重大违法","退市风险","监管措施"]),
 (0,4.0,"EXECUTION_RISK",["终止项目","终止投资","项目终止","停止建设","取消项目"]),
 (1,3.5,"EXECUTION_RISK",["项目延期","延期投产","推迟投产","建设延期","进度不及预期","未达预期"]),
 (0,4.0,"RISK",["事故","火灾","爆炸","停产","暂停生产","重大诉讼","重大仲裁","查封","冻结"]),
 (9,4.0,"EARNINGS",["业绩预减","预亏","由盈转亏","业绩下修","下调业绩","下修业绩"]),]
PROJECT_PATTERNS=[re.compile(r"([\u4e00-\u9fa5A-Za-z0-9·\-]{2,28}(?:项目|工程|生产线|装置|基地))"),re.compile(r"(?:建设|投资|实施)([\u4e00-\u9fa5A-Za-z0-9·\-]{2,24})")]
PROGRESS_PATTERNS=[re.compile(r"(?:建设进度|项目进度|工程进度|已完成|完成比例|完成进度|总体进度)[^。；，,]{0,18}?([0-9]{1,3}(?:\.[0-9]+)?)%"),re.compile(r"([0-9]{1,3}(?:\.[0-9]+)?)%[^。；，,]{0,12}(?:已完成|完成|建设完成)")]
CAPACITY_RE=re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(万吨|吨)\s*/?\s*(?:年|a)?")
AMOUNT_RE=re.compile(r"(?:金额|投资额|总投资|合同价款|合同金额|订单金额|中标金额)[^。；，,]{0,20}?([0-9]+(?:\.[0-9]+)?)\s*(亿元|万元|元)")
DATE_RE=re.compile(r"(?:预计|计划|力争|拟于|争取)[^。；]{0,16}?((?:20)?[0-9]{2}年(?:[0-9]{1,2}月)?(?:[0-9]{1,2}日)?)")

def http_session():
 s=requests.Session(); retry=Retry(total=4,connect=4,read=4,backoff_factor=.5,status_forcelist=(429,500,502,503,504),allowed_methods=frozenset(["GET"]))
 s.mount("https://",HTTPAdapter(max_retries=retry,pool_connections=16,pool_maxsize=16)); s.headers.update({"User-Agent":"Mozilla/5.0 pesticide-quant-fulltext-v2","Referer":"https://data.eastmoney.com/","Accept":"application/json,text/plain,*/*"}); return s

def strip_text(x):
 x=html.unescape(str(x or "")); x=re.sub(r"<[^>]+>"," ",x); x=unicodedata.normalize("NFKC",x); x=re.sub(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f]"," ",x); x=re.sub(r"[ \t\r\f\v]+"," ",x); x=re.sub(r"\n{3,}","\n\n",x); return x.strip()

def fetch_one(art_code,timeout=15.0):
 s=http_session()
 try:
  r=s.get(CONTENT_URL,params={"art_code":art_code,"client_source":"web","page_index":1,"show_all":1},timeout=timeout); r.raise_for_status()
  try: payload=r.json()
  except Exception:
   m=re.search(r"\((\{.*\})\)\s*$",r.text,re.S)
   if not m: raise
   payload=json.loads(m.group(1))
  d=payload.get("data") or {}; txt=strip_text(d.get("notice_content") or d.get("content") or ""); attach=d.get("attach_url_web") or d.get("attach_url") or ""
  return {"art_code":art_code,"ok":bool(txt),"text":txt,"attach_url":attach,"raw_meta":json.dumps({k:d.get(k) for k in ["notice_title","short_name","page_size","total_page","attach_url","attach_url_web"]},ensure_ascii=False)}
 except Exception as exc: return {"art_code":art_code,"ok":False,"text":"","attach_url":"","raw_meta":repr(exc)}
 finally: s.close()

def ensure_schema(con):
 con.executescript("""
 CREATE TABLE IF NOT EXISTS announcement_fulltext_v2(art_code TEXT PRIMARY KEY,canonical_code TEXT NOT NULL,notice_date TEXT NOT NULL,effective_date TEXT,title TEXT NOT NULL,full_text TEXT,text_chars INTEGER,attach_url TEXT,fetch_status TEXT,fetch_meta TEXT,fetched_at TEXT,classification_version TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS announcement_event_v2(canonical_code TEXT NOT NULL,art_code TEXT PRIMARY KEY,notice_date TEXT NOT NULL,effective_date TEXT,title TEXT NOT NULL,topic_key TEXT NOT NULL,category_v2 TEXT NOT NULL,stage_v2 INTEGER NOT NULL,direction_v2 INTEGER NOT NULL,hardness_v2 REAL NOT NULL,progress_pct REAL,capacity_tons REAL,amount_cny REAL,promised_date_text TEXT,stage_delta REAL,progress_delta REAL,days_since_prev REAL,long_setup_flag REAL,recent_acceleration_event REAL,negative_acceleration_event REAL,novelty_v2 REAL,chain_confidence_v2 REAL,content_signature TEXT,classification_version TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS announcement_feature_daily_v2(trade_date TEXT NOT NULL,code TEXT NOT NULL,ft_long_term_setup_score REAL,ft_recent_acceleration_30d REAL,ft_recent_acceleration_90d REAL,ft_milestone_delta_90d REAL,ft_progress_delta_90d REAL,ft_commercialization_90d REAL,ft_negative_acceleration_90d REAL,ft_topic_depth_365d REAL,ft_chain_confidence REAL,ft_text_novelty_90d REAL,ft_price_digestion_score REAL,ft_event_excess_ret_since_key REAL,ft_information_price_gap REAL,ft_days_since_inflection REAL,ft_event_count_90d INTEGER,ft_available REAL,source_id TEXT NOT NULL,PRIMARY KEY(trade_date,code));
 """); con.commit()

def candidate_rows(con,max_rows):
 x=pd.read_sql_query("""SELECT e.canonical_code,e.art_code,e.notice_date,e.effective_date,e.title,e.category,e.stage,e.direction,e.hardness,r.columns_json FROM announcement_event e JOIN announcement_raw r ON r.canonical_code=e.canonical_code AND r.art_code=e.art_code WHERE e.effective_date IS NOT NULL ORDER BY e.notice_date,e.canonical_code,e.art_code""",con)
 if x.empty:return x
 title=x.title.fillna(""); cols=x.columns_json.fillna(""); skip=title.apply(lambda s:any(k in s for k in SKIP_TITLE)); hinted=title.apply(lambda s:any(k in s for k in CANDIDATE_HINTS))|cols.apply(lambda s:any(k in s for k in CANDIDATE_HINTS)); already=x.category.fillna("OTHER").ne("OTHER"); x=x[(~skip)&(hinted|already)].copy()
 return x.tail(max_rows).copy() if max_rows else x

def topic_key(title,text,category):
 probe=strip_text(title+"\n"+text[:8000]); cand=[]
 for pat in PROJECT_PATTERNS:cand.extend(m.group(1) for m in pat.finditer(probe))
 banned=("本项目","该项目","公司项目","投资项目","建设项目","募集资金投资项目"); cand=[re.sub(r"^(关于|公司|本公司|拟|投资|建设)","",c) for c in cand]; cand=[c for c in cand if len(c)>=3 and c not in banned]
 if cand:
  vc=pd.Series(cand).value_counts(); mx=vc.max(); best=sorted([k for k,v in vc.items() if v==mx],key=len,reverse=True)[0]; return f"PROJECT|{best[:36]}"
 compact=re.sub(r"关于|公告|进展|情况|提示性|公司|股份有限公司|董事会|监事会","",strip_text(title)); compact=re.sub(r"20\d{2}年|第[一二三四五六七八九十\d]+次","",compact); return f"{category}|{compact[:40]}"

def extract_progress(text):
 vals=[]
 for pat in PROGRESS_PATTERNS:
  for m in pat.finditer(text):
   try:
    v=float(m.group(1)); vals.append(v) if 0<=v<=100 else None
   except Exception: pass
 return max(vals) if vals else None

def extract_capacity(text):
 vals=[]
 for m in CAPACITY_RE.finditer(text):
  try:
   v=float(m.group(1))*(10000 if m.group(2)=="万吨" else 1); vals.append(v) if v>0 else None
  except Exception:pass
 return max(vals) if vals else None

def extract_amount(text):
 vals=[]; unit={"亿元":1e8,"万元":1e4,"元":1}
 for m in AMOUNT_RE.finditer(text):
  try:
   v=float(m.group(1))*unit[m.group(2)]; vals.append(v) if v>0 else None
  except Exception:pass
 return max(vals) if vals else None

def classify_full(title,text,title_row):
 probe=strip_text(title+"\n"+text[:20000])
 for stage,hard,cat,kws in NEG_RULES:
  if any(k in probe for k in kws):return cat,stage,-1,hard
 best=None
 for stage,hard,cat,kws in POS_RULES:
  if any(k in probe for k in kws):
   cand=(stage,hard,cat); best=cand if best is None or cand[0]>best[0] or (cand[0]==best[0] and cand[1]>best[1]) else best
 if best:return best[2],best[0],1,best[1]
 return str(title_row.category),int(title_row.stage),int(title_row.direction),float(title_row.hardness)

def build_event_v2(src,full):
 if src.empty:return pd.DataFrame()
 by=full.set_index("art_code") if not full.empty else pd.DataFrame(); rows=[]
 for r in src.itertuples(index=False):
  text=str(by.loc[r.art_code,"full_text"] or "") if len(full) and r.art_code in by.index else ""; cat,stage,direction,hard=classify_full(r.title,text,pd.Series(r._asdict())); probe=strip_text(r.title+"\n"+text[:25000]); prog=extract_progress(probe); cap=extract_capacity(probe); amt=extract_amount(probe); dm=DATE_RE.search(probe); prom=dm.group(1) if dm else None; topic=topic_key(r.title,text,cat); sig=hashlib.sha1(re.sub(r"\s+","",probe[:12000]).encode("utf-8","ignore")).hexdigest()[:20]
  rows.append({"canonical_code":r.canonical_code,"art_code":r.art_code,"notice_date":r.notice_date,"effective_date":r.effective_date,"title":r.title,"topic_key":topic,"category_v2":cat,"stage_v2":stage,"direction_v2":direction,"hardness_v2":hard,"progress_pct":prog,"capacity_tons":cap,"amount_cny":amt,"promised_date_text":prom,"content_signature":sig,"classification_version":VERSION})
 ev=pd.DataFrame(rows); ev.notice_date=pd.to_datetime(ev.notice_date); ev.effective_date=pd.to_datetime(ev.effective_date); ev=ev.sort_values(["canonical_code","topic_key","effective_date","art_code"]).reset_index(drop=True)
 for c,val in [("stage_delta",0.0),("progress_delta",0.0),("days_since_prev",np.nan),("long_setup_flag",0.0),("recent_acceleration_event",0.0),("negative_acceleration_event",0.0),("novelty_v2",1.0),("chain_confidence_v2",0.0)]:ev[c]=val
 for _,idx in ev.groupby(["canonical_code","topic_key"],sort=False).groups.items():
  ids=list(idx); prev=None; prior=[]; sig_dates={}
  for j in ids:
   rr=ev.loc[j]; d=pd.Timestamp(rr.effective_date)
   if prev is not None:
    ev.loc[j,"days_since_prev"]=(d-pd.Timestamp(ev.loc[prev,"effective_date"])).days; ev.loc[j,"stage_delta"]=float(rr.stage_v2)-float(ev.loc[prev,"stage_v2"]); a=rr.progress_pct; b=ev.loc[prev,"progress_pct"]
    if pd.notna(a) and pd.notna(b):ev.loc[j,"progress_delta"]=float(a)-float(b)
   ev.loc[j,"long_setup_flag"]=1.0 if any(91<=(d-p).days<=540 for p in prior) else 0.0; sd=sig_dates.get(rr.content_signature,[]); reps=sum(1 for p in sd if 0<=(d-p).days<=180); ev.loc[j,"novelty_v2"]=1/(1+reps); sig_dates.setdefault(rr.content_signature,[]).append(d)
   stage_delta=max(float(ev.loc[j,"stage_delta"]),0.0); prog_delta=max(float(ev.loc[j,"progress_delta"]),0.0); base=stage_delta*float(rr.hardness_v2)+min(prog_delta/20,3)
   if float(rr.direction_v2)>0:ev.loc[j,"recent_acceleration_event"]=base*(1.25 if ev.loc[j,"long_setup_flag"] else 1)*float(ev.loc[j,"novelty_v2"])
   elif float(rr.direction_v2)<0:ev.loc[j,"negative_acceleration_event"]=(float(rr.hardness_v2)+abs(min(float(ev.loc[j,"stage_delta"]),0)))*float(ev.loc[j,"novelty_v2"])
   chain_len=sum(1 for p in prior if 0<=(d-p).days<=540)+1; ev.loc[j,"chain_confidence_v2"]=min(1,.22*chain_len+.16*(float(rr.hardness_v2)>=3)+.18*(pd.notna(rr.progress_pct) or pd.notna(rr.amount_cny))); prior.append(d); prev=j
 ev.notice_date=ev.notice_date.dt.date.astype(str); ev.effective_date=ev.effective_date.dt.date.astype(str); return ev

def industry_log_return(market,membership):
 z=market[["trade_date","code","close_qfq"]].copy(); z["ret1"]=z.groupby("code").close_qfq.pct_change(fill_method=None); parts=[]; by={k:g for k,g in z.groupby("code")}; mm=membership.copy(); mm.in_date=pd.to_datetime(mm.in_date); mm.out_date=pd.to_datetime(mm.out_date,errors="coerce")
 for code,gmem in mm.groupby("code"):
  g=by.get(code)
  if g is None:continue
  for r in gmem.itertuples(index=False):
   mask=g.trade_date.ge(r.in_date); mask &= g.trade_date.le(r.out_date) if pd.notna(r.out_date) else True
   if mask.any():parts.append(g.loc[mask,["trade_date","ret1"]])
 if not parts:return pd.Series(dtype=float)
 ret=pd.concat(parts).groupby("trade_date").ret1.mean().sort_index().clip(lower=-.999); return np.log1p(ret).fillna(0).cumsum()

def score_daily(ev,market,membership):
 if ev.empty or market.empty:return pd.DataFrame()
 e=ev.copy(); e.effective_date=pd.to_datetime(e.effective_date); m=market.copy(); m.trade_date=pd.to_datetime(m.trade_date); m=m.sort_values(["code","trade_date"]); ind_log=industry_log_return(m,membership); ev_by={k:g.sort_values("effective_date") for k,g in e.groupby("canonical_code")}; rows=[]
 for code,g in m.groupby("code",sort=False):
  histall=ev_by.get(code)
  if histall is None or histall.empty:continue
  g=g.sort_values("trade_date"); stock_log=np.log(pd.Series(pd.to_numeric(g.close_qfq,errors="coerce").to_numpy(),index=g.trade_date)).replace([np.inf,-np.inf],np.nan).ffill()
  for d in g.trade_date:
   hist=histall[histall.effective_date.le(d)]
   if hist.empty:continue
   age=(d-hist.effective_date).dt.days; e30=hist[(age>=0)&(age<=30)]; e90=hist[(age>=0)&(age<=90)]; y=hist[(age>=0)&(age<=365)]; old=hist[(age>=91)&(age<=540)]; pos=e90[e90.direction_v2>0]; neg=e90[e90.direction_v2<0]
   accel30=float(e30.recent_acceleration_event.sum()); accel90=float(e90.recent_acceleration_event.sum()); neg90=float(e90.negative_acceleration_event.sum()); setup=float(old.long_setup_flag.sum()+((old.category_v2=="PROJECT")&(old.stage_v2<=5)).sum()*.5); milestone=float(pos.stage_delta.clip(lower=0).mul(pos.hardness_v2).sum()) if len(pos) else 0.; prog=float(pos.progress_delta.clip(lower=0).sum()) if len(pos) else 0.; commercial=float(pos[pos.category_v2.eq("COMMERCIAL")].hardness_v2.mul(pos.novelty_v2).sum()) if len(pos) else 0.; topic_depth=float(y.topic_key.nunique()); conf=float(e90.chain_confidence_v2.mean()) if len(e90) else 0.; novelty=float(e90.novelty_v2.mean()) if len(e90) else 0.; key=hist[(hist.hardness_v2>=3)&(hist.direction_v2!=0)]; digestion=np.nan; excess=np.nan; days=np.nan; signed_info=math.tanh((accel90+commercial-neg90)/6)
   if len(key):
    kr=key.iloc[-1]; kd=pd.Timestamp(kr.effective_date); days=float((d-kd).days)
    if kd in stock_log.index and d in stock_log.index and kd in ind_log.index and d in ind_log.index:
     sret=float(np.exp(stock_log.loc[d]-stock_log.loc[kd])-1); iret=float(np.exp(ind_log.loc[d]-ind_log.loc[kd])-1); excess=sret-iret; digestion=float(np.clip(float(kr.direction_v2)*excess/.30,0,1.5))
   discount=1-min(float(digestion) if np.isfinite(digestion) else 0,1); gap=float(signed_info*discount)
   rows.append({"trade_date":d.date().isoformat(),"code":code,"ft_long_term_setup_score":setup,"ft_recent_acceleration_30d":accel30,"ft_recent_acceleration_90d":accel90,"ft_milestone_delta_90d":milestone,"ft_progress_delta_90d":prog,"ft_commercialization_90d":commercial,"ft_negative_acceleration_90d":neg90,"ft_topic_depth_365d":topic_depth,"ft_chain_confidence":conf,"ft_text_novelty_90d":novelty,"ft_price_digestion_score":digestion,"ft_event_excess_ret_since_key":excess,"ft_information_price_gap":gap,"ft_days_since_inflection":days,"ft_event_count_90d":int(len(e90)),"ft_available":1.,"source_id":SOURCE_ID})
 return pd.DataFrame(rows)

def main():
 ap=argparse.ArgumentParser(); ap.add_argument("--db",required=True); ap.add_argument("--workers",type=int,default=6); ap.add_argument("--max-rows",type=int,default=0); ap.add_argument("--audit-json"); ap.add_argument("--event-csv"); ap.add_argument("--feature-csv"); args=ap.parse_args(); con=sqlite3.connect(args.db); ensure_schema(con)
 try:
  src=candidate_rows(con,args.max_rows or None)
  if src.empty:raise SystemExit("No full-text candidates")
  existing=pd.read_sql_query("SELECT art_code,full_text,attach_url,fetch_status,fetch_meta FROM announcement_fulltext_v2",con); have=set(existing.loc[existing.fetch_status.eq("OK"),"art_code"]) if len(existing) else set(); todo=[a for a in src.art_code.astype(str).unique() if a not in have]; print("FULLTEXT_CANDIDATES",len(src),"TODO",len(todo),"WORKERS",args.workers); fetched=[]
  with cf.ThreadPoolExecutor(max_workers=max(1,args.workers)) as ex:
   fut={ex.submit(fetch_one,a):a for a in todo}
   for i,f in enumerate(cf.as_completed(fut),1):
    res=f.result(); fetched.append(res)
    if i%100==0 or i==len(todo):print("FULLTEXT",i,len(todo),"ok",sum(x["ok"] for x in fetched))
  now=dt.datetime.now().isoformat(timespec="seconds"); src_idx=src.set_index("art_code")
  for r in fetched:
   rr=src_idx.loc[r["art_code"]]; rr=rr.iloc[0] if isinstance(rr,pd.DataFrame) else rr
   con.execute("INSERT OR REPLACE INTO announcement_fulltext_v2(art_code,canonical_code,notice_date,effective_date,title,full_text,text_chars,attach_url,fetch_status,fetch_meta,fetched_at,classification_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(r["art_code"],rr.canonical_code,rr.notice_date,rr.effective_date,rr.title,r["text"],len(r["text"]),r["attach_url"],"OK" if r["ok"] else "FAILED",r["raw_meta"],now,VERSION))
  con.commit(); full=pd.read_sql_query("SELECT art_code,full_text,attach_url,fetch_status FROM announcement_fulltext_v2",con); full=full[full.art_code.isin(src.art_code)].copy(); ev=build_event_v2(src,full); con.execute("DELETE FROM announcement_event_v2"); ev.to_sql("announcement_event_v2",con,if_exists="append",index=False) if len(ev) else None; market=pd.read_sql_query("SELECT trade_date,code,close_qfq FROM market_daily WHERE close_qfq IS NOT NULL ORDER BY code,trade_date",con); membership=pd.read_sql_query("SELECT code,in_date,out_date FROM industry_membership_history",con); feat=score_daily(ev,market,membership); con.execute("DELETE FROM announcement_feature_daily_v2 WHERE source_id=?",(SOURCE_ID,)); feat.to_sql("announcement_feature_daily_v2",con,if_exists="append",index=False) if len(feat) else None
  con.execute("INSERT OR REPLACE INTO source_registry(source_id,data_layer,source_name,url,coverage,source_type,reliability,v1_status,notes) VALUES(?,?,?,?,?,?,?,?,?)",(SOURCE_ID,"announcement_fulltext","Eastmoney announcement full text",CONTENT_URL,f"candidate disclosures; {len(src)} art codes","EASTMONEY_CONTENT_API","MEDIUM","FULLTEXT_V2_BUILT","same-topic milestone/progress acceleration; title V1.1 retained as separate baseline; PIT uses pre-audited effective_date")); con.commit(); ok=int((full.fetch_status=="OK").sum()) if len(full) else 0; failed=int((full.fetch_status!="OK").sum()) if len(full) else 0
  audit={"source":SOURCE_ID,"version":VERSION,"candidate_rows":int(len(src)),"candidate_codes":int(src.canonical_code.nunique()),"fulltext_ok":ok,"fulltext_failed":failed,"fulltext_coverage":ok/max(len(full),1),"event_rows":int(len(ev)),"event_codes":int(ev.canonical_code.nunique()) if len(ev) else 0,"events_with_stage_delta":int((ev.stage_delta!=0).sum()) if len(ev) else 0,"events_with_progress":int(ev.progress_pct.notna().sum()) if len(ev) else 0,"long_setup_events":int((ev.long_setup_flag>0).sum()) if len(ev) else 0,"positive_acceleration_events":int((ev.recent_acceleration_event>0).sum()) if len(ev) else 0,"negative_acceleration_events":int((ev.negative_acceleration_event>0).sum()) if len(ev) else 0,"feature_rows":int(len(feat)),"feature_codes":int(feat.code.nunique()) if len(feat) else 0,"pit_rule":"inherits audited title-event effective_date; never earlier than next market trading day","scope_note":"periodic reports/routine governance excluded; financial statements remain in PIT finance layer"}
  if args.audit_json:Path(args.audit_json).write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding="utf-8")
  if args.event_csv and len(ev):ev.to_csv(args.event_csv,index=False,encoding="utf-8-sig")
  if args.feature_csv and len(feat):feat.to_csv(args.feature_csv,index=False,encoding="utf-8-sig")
  print(json.dumps(audit,ensure_ascii=False,indent=2))
  if ok==0:raise SystemExit(2)
 finally:con.close()
if __name__=="__main__":main()
