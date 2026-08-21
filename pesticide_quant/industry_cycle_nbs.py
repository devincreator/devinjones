#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build PIT glyphosate industry-cycle series from official NBS releases.

Source: 国家统计局《流通领域重要生产资料市场价格变动情况》
Product: 农药（草甘膦，95%原药）

Critical PIT rule:
`industry_product_daily.date` is the PUBLICATION date, not the observation
period. A price for "2022年1月下旬" is unavailable to the model until NBS
publishes that release.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import re
import sqlite3
import time
from io import StringIO
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SEARCH = "https://www.stats.gov.cn/search/s"
SOURCE_ID = "S019"
PRODUCT_ID = "NBS_GLYPHOSATE_95"
PRODUCT_NAME = "农药（草甘膦，95%原药）"


def session():
    s = requests.Session()
    retry = Retry(total=3, connect=3, read=3, backoff_factor=0.7,
                  status_forcelist=(429,500,502,503,504), allowed_methods=frozenset(["GET"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent":"Mozilla/5.0 pesticide-quant-github-actions",
                      "Accept-Language":"zh-CN,zh;q=0.9,en;q=0.5"})
    return s


def period_titles(start: dt.date, end: dt.date):
    cur = dt.date(start.year, start.month, 1)
    while cur <= end:
        for label, day in [("上旬",10),("中旬",20),("下旬",calendar.monthrange(cur.year,cur.month)[1])]:
            period_end = dt.date(cur.year, cur.month, min(day, calendar.monthrange(cur.year,cur.month)[1]))
            if period_end < start or period_end > end:
                continue
            title = f"{cur.year}年{cur.month}月{label}流通领域重要生产资料市场价格变动情况"
            yield title, f"{cur.year}-{cur.month:02d}-{label}", period_end
        if cur.month == 12:
            cur = dt.date(cur.year+1,1,1)
        else:
            cur = dt.date(cur.year,cur.month+1,1)


def find_release_url(s, title):
    r = s.get(SEARCH, params={"qt":title,"siteCode":"bm36000002","tab":"all"}, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    exact=[]; broad=[]
    for a in soup.find_all("a", href=True):
        text=" ".join(a.stripped_strings)
        href=urljoin(r.url,a.get("href"))
        if "stats.gov.cn" not in href:
            continue
        if title in text:
            exact.append(href)
        elif "流通领域重要生产资料市场价格变动情况" in text:
            broad.append(href)
    candidates=exact+broad
    seen=[]
    for u in candidates:
        if u not in seen:seen.append(u)
    return seen[0] if seen else None


def parse_publish_date(html):
    soup=BeautifulSoup(html,"lxml")
    # Prefer metadata / clearly labelled publication time.
    for meta in soup.find_all("meta"):
        name=(meta.get("name") or meta.get("property") or "").lower()
        val=meta.get("content") or ""
        if any(k in name for k in ["pub","publish","date","time"]):
            m=re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})",val)
            if m:
                return dt.date(int(m.group(1)),int(m.group(2)),int(m.group(3)))
    text=" ".join(soup.stripped_strings)
    # Search near the start first; release pages normally print date below title.
    for chunk in [text[:2500], text]:
        m=re.search(r"(20\d{2})[年\-/\.](\d{1,2})[月\-/\.](\d{1,2})日?",chunk)
        if m:
            try:return dt.date(int(m.group(1)),int(m.group(2)),int(m.group(3)))
            except ValueError:pass
    return None


def parse_price(html):
    try:
        tables=pd.read_html(StringIO(html))
    except Exception:
        tables=[]
    for df in tables:
        for _,row in df.iterrows():
            vals=["" if pd.isna(x) else str(x).strip() for x in row.tolist()]
            joined=" ".join(vals)
            if "草甘膦" not in joined or "农药" not in joined:
                continue
            # Usually: product | unit | current price | change | pct.
            after_unit=False
            for v in vals:
                if v == "吨" or "吨" == v.strip():
                    after_unit=True;continue
                if not after_unit:continue
                z=v.replace(",","")
                try:return float(z)
                except Exception:pass
    # HTML/text fallback: number after 吨 near product phrase.
    soup=BeautifulSoup(html,"lxml")
    text=" ".join(soup.stripped_strings)
    m=re.search(r"农药[（(]草甘膦[^）)]*[）)]\s*吨\s*([0-9,]+(?:\.\d+)?)",text)
    return float(m.group(1).replace(",","")) if m else None


def main():
    ap=argparse.ArgumentParser();ap.add_argument("--db",required=True);ap.add_argument("--start",default="2020-06-01");ap.add_argument("--end",default=dt.date.today().isoformat());ap.add_argument("--audit-json",default="work/industry_cycle_nbs_audit.json");args=ap.parse_args()
    start=dt.date.fromisoformat(args.start);end=dt.date.fromisoformat(args.end)
    s=session();con=sqlite3.connect(args.db);now=dt.datetime.now().isoformat(timespec="seconds")
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS industry_product_observation(
            product_id TEXT NOT NULL,observation_period TEXT NOT NULL,period_end TEXT,
            publication_date TEXT,price REAL,unit TEXT,source_url TEXT,status TEXT,ingest_ts TEXT,
            PRIMARY KEY(product_id,observation_period))""")
        con.execute("""INSERT OR REPLACE INTO source_registry(
            source_id,data_layer,source_name,url,coverage,source_type,reliability,v1_status,notes)
            VALUES(?,?,?,?,?,?,?,?,?)""",(
            SOURCE_ID,"industry_product_daily","国家统计局流通领域草甘膦95%原药价格",
            "https://www.stats.gov.cn/search/s","2020-06-01 onward, 旬度 releases","NBS_OFFICIAL_WEB","HIGH","FETCHING",
            "PIT date uses publication_date, never observation period"
        ))
        con.execute("DELETE FROM industry_product_daily WHERE source_id=?",(SOURCE_ID,))
        con.execute("DELETE FROM industry_product_observation WHERE product_id=?",(PRODUCT_ID,))
        con.commit()
        ok=[];miss=[]
        for i,(title,period,period_end) in enumerate(period_titles(start,end),1):
            try:url=find_release_url(s,title)
            except Exception as e:
                print("NBS_SEARCH_ERR",period,repr(e));miss.append({"period":period,"reason":"search_error"});continue
            if not url:
                print("NBS_NO_RESULT",period);miss.append({"period":period,"reason":"no_result"});continue
            try:
                r=s.get(url,timeout=25);r.raise_for_status();html=r.text
                pub=parse_publish_date(html);price=parse_price(html)
            except Exception as e:
                print("NBS_PAGE_ERR",period,url,repr(e));miss.append({"period":period,"reason":"page_error","url":url});continue
            if not pub or price is None:
                print("NBS_PARSE_MISS",period,"pub",pub,"price",price,url)
                con.execute("INSERT OR REPLACE INTO industry_product_observation VALUES(?,?,?,?,?,?,?,?,?)",
                            (PRODUCT_ID,period,period_end.isoformat(),pub.isoformat() if pub else None,price,"元/吨",url,"PARSE_MISS",now))
                con.commit();miss.append({"period":period,"reason":"parse_miss","url":url});continue
            # Publication date must not precede observation period end by more than a few days.
            if pub < period_end - dt.timedelta(days=5):
                print("NBS_DATE_REJECT",period,period_end,pub,url);miss.append({"period":period,"reason":"bad_publish_date","url":url});continue
            con.execute("INSERT OR REPLACE INTO industry_product_observation VALUES(?,?,?,?,?,?,?,?,?)",
                        (PRODUCT_ID,period,period_end.isoformat(),pub.isoformat(),price,"元/吨",url,"OK",now))
            con.execute("""INSERT OR REPLACE INTO industry_product_daily(
                date,product_id,product_name,category,price,unit,region,frequency,source_id,ingest_ts)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (pub.isoformat(),PRODUCT_ID,PRODUCT_NAME,"农药原药",price,"元/吨","全国","旬度",SOURCE_ID,now))
            con.commit();ok.append({"period":period,"publication_date":pub.isoformat(),"price":price,"url":url})
            print("NBS_OK",period,pub,price,url)
            time.sleep(0.08)
        mn,mx=con.execute("SELECT MIN(date),MAX(date) FROM industry_product_daily WHERE source_id=?",(SOURCE_ID,)).fetchone()
        audit={"rows":len(ok),"missing":len(miss),"min_publication_date":mn,"max_publication_date":mx,"missing_samples":miss[:30]}
        Path(args.audit_json).parent.mkdir(parents=True,exist_ok=True);Path(args.audit_json).write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding="utf-8")
        con.execute("UPDATE source_registry SET v1_status=?,notes=notes||? WHERE source_id=?",
                    ("FETCHED" if ok else "FAILED",f"; rows={len(ok)} missing={len(miss)} min={mn} max={mx}",SOURCE_ID));con.commit()
        print(json.dumps(audit,ensure_ascii=False,indent=2))
        if len(ok)<30:raise SystemExit(2)
    finally:con.close()


if __name__=="__main__":main()
