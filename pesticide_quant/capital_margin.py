#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch historical individual-stock margin financing data from Eastmoney.

Endpoint: datacenter-web.eastmoney.com/api/data/v1/get
reportName: RPTA_WEB_RZRQ_GGMX

This is a capital-flow feature source. Absence of rows does not mean zero
financing; it can mean the security was not margin-eligible or the vendor has no
coverage, so missing values remain missing.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sqlite3
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SOURCE_ID = "S018"
ALIASES = {
    "920819": ["833819", "920819"],
    "920866": ["870866", "920866"],
}


def session():
    s = requests.Session()
    retry = Retry(total=4, connect=4, read=4, backoff_factor=0.8,
                  status_forcelist=(429,500,502,503,504), allowed_methods=frozenset(["GET"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent":"Mozilla/5.0 pesticide-quant-github-actions",
                      "Referer":"https://data.eastmoney.com/rzrq/","Accept":"application/json,*/*"})
    return s


def universe(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return sorted({r["code"].strip() for r in csv.DictReader(f) if r.get("code","").strip()})


def num(d, key):
    v = d.get(key)
    if v in (None, "", "-"):
        return None
    try:
        return float(v)
    except Exception:
        return None


def fetch_code(s, code):
    rows = []
    page = 1
    while True:
        r = s.get(URL, params={
            "reportName":"RPTA_WEB_RZRQ_GGMX",
            "columns":"ALL",
            "filter":f'(SCODE="{code}")',
            "sortColumns":"DATE",
            "sortTypes":"-1",
            "pageNumber":page,
            "pageSize":500,
            "source":"WEB",
            "client":"WEB",
        }, timeout=30)
        r.raise_for_status()
        result = (r.json().get("result") or {})
        data = result.get("data") or []
        rows.extend(data)
        pages = int(result.get("pages") or 1)
        if not data or page >= pages:
            break
        page += 1
        time.sleep(0.05)
    return rows


def canonical_rows(s, canonical):
    by = {}
    for src in ALIASES.get(canonical, [canonical]):
        try:
            rows = fetch_code(s, src)
        except Exception as e:
            print("MARGIN_ERR", canonical, src, repr(e))
            continue
        print("MARGIN_SOURCE", canonical, src, len(rows))
        for d in rows:
            day = str(d.get("DATE") or "")[:10]
            if day:
                by[day] = d
    return [by[k] for k in sorted(by)]


def main():
    ap = argparse.ArgumentParser();ap.add_argument("--db",required=True);ap.add_argument("--universe-csv",required=True);args=ap.parse_args()
    codes = universe(args.universe_csv)
    s = session(); con = sqlite3.connect(args.db); now = dt.datetime.now().isoformat(timespec="seconds")
    try:
        con.execute("""INSERT OR REPLACE INTO source_registry(
        source_id,data_layer,source_name,url,coverage,source_type,reliability,v1_status,notes)
        VALUES(?,?,?,?,?,?,?,?,?)""",(
            SOURCE_ID,"capital_flow_daily","Eastmoney individual margin trading history",URL,
            "RPTA_WEB_RZRQ_GGMX by SCODE","EASTMONEY_DATACENTER","MEDIUM","FETCHING",
            "RZYE/RZMRE/RZCHE/RQYE; missing means unavailable/not margin-eligible, never coerced to zero"
        ))
        con.execute("DELETE FROM capital_flow_daily WHERE source_id=?", (SOURCE_ID,))
        total = 0; loaded = 0
        for i, code in enumerate(codes, 1):
            rows = canonical_rows(s, code)
            local = 0
            for d in rows:
                day = str(d.get("DATE") or "")[:10]
                if not day:
                    continue
                con.execute("""INSERT INTO capital_flow_daily(
                    trade_date,code,margin_balance_cny,margin_buy_cny,margin_repay_cny,short_balance_cny,source_id,ingest_ts
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(trade_date,code) DO UPDATE SET
                    margin_balance_cny=excluded.margin_balance_cny,
                    margin_buy_cny=excluded.margin_buy_cny,
                    margin_repay_cny=excluded.margin_repay_cny,
                    short_balance_cny=excluded.short_balance_cny,
                    source_id=excluded.source_id,ingest_ts=excluded.ingest_ts""",
                    (day,code,num(d,"RZYE"),num(d,"RZMRE"),num(d,"RZCHE"),num(d,"RQYE"),SOURCE_ID,now))
                local += 1; total += 1
            con.commit()
            if local: loaded += 1
            print("MARGIN", i, len(codes), code, local)
            time.sleep(0.05)
        mn,mx=con.execute("SELECT MIN(trade_date),MAX(trade_date) FROM capital_flow_daily WHERE source_id=?",(SOURCE_ID,)).fetchone()
        con.execute("UPDATE source_registry SET v1_status='FETCHED',notes=notes||? WHERE source_id=?",
                    (f"; rows={total} codes={loaded} min={mn} max={mx}",SOURCE_ID))
        con.commit()
        print({"target_codes":len(codes),"loaded_codes":loaded,"rows":total,"min":mn,"max":mx})
        if total == 0:
            raise SystemExit(2)
    finally:
        con.close()


if __name__=="__main__":main()
