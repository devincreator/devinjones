#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PIT company-announcement event-chain intelligence for the pesticide universe.

Purpose
-------
The 60-trading-day model should not treat announcements as isolated positive/
negative headlines.  This module reconstructs a company disclosure process:
long-term setup -> recent acceleration -> hard milestone/commercialisation ->
price digestion.

V1 deliberately uses announcement LIST metadata/title only.  It is conservative:
* every disclosure becomes tradable on the NEXT observed trading day;
* repeated/near-duplicate event families are down-weighted;
* no sentiment is invented when a title does not reveal direction;
* title-only classification is explicitly tagged and must be upgraded with full
  text before the announcement layer can be called final.

Source: Eastmoney announcement-list API.  Raw source rows are preserved.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
import sqlite3
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
SOURCE_ID = "S020"

ALIASES = {
    "920819": ["833819", "920819"],
    "920866": ["870866", "920866"],
}

# category, stage(0-9), direction(-1/0/+1), hardness(1-4), keywords
RULES = [
    ("REGULATORY", 0, -1, 4, ["立案", "行政处罚", "处罚决定", "退市风险", "重大违法", "监管措施"]),
    ("RISK", 0, -1, 4, ["事故", "火灾", "爆炸", "停产", "暂停生产", "查封", "冻结", "重大诉讼", "仲裁"]),
    ("RISK_RECOVERY", 7, +1, 4, ["复产", "恢复生产", "解除停产", "整改完成"]),
    ("PROJECT", 7, +1, 4, ["正式投产", "投产运行", "竣工投产", "投入生产", "达产"]),
    ("PROJECT", 6, +1, 4, ["试生产", "试运行"]),
    ("PROJECT", 5, +1, 3, ["竣工", "设备安装完成", "建设完成"]),
    ("PROJECT", 4, +1, 3, ["开工", "建设进展", "项目进展", "工程进展"]),
    ("PROJECT", 3, +1, 3, ["环评批复", "环境影响评价批复", "取得批复", "取得许可", "取得备案", "获批"]),
    ("PROJECT", 2, +1, 2, ["签署投资协议", "投资建设", "对外投资", "项目投资", "增资扩产"]),
    ("PROJECT", 1, 0, 1, ["拟投资", "投资计划", "规划建设", "项目规划"]),
    ("COMMERCIAL", 9, +1, 4, ["重大合同", "签订合同", "签署合同", "中标", "获得订单", "订单"]),
    ("COMMERCIAL", 8, +1, 3, ["客户认证", "产品认证", "取得登记证", "农药登记证", "获得登记"]),
    ("EARNINGS", 9, +1, 4, ["业绩预增", "大幅预增", "扭亏为盈"]),
    ("EARNINGS", 9, -1, 4, ["业绩预减", "预亏", "由盈转亏", "业绩下修"]),
    ("EARNINGS", 8, 0, 3, ["业绩预告修正", "业绩预告", "业绩快报"]),
    ("BUYBACK", 7, +1, 3, ["回购股份", "股份回购", "完成回购"]),
    ("HOLDER", 6, +1, 3, ["增持计划", "增持股份", "完成增持"]),
    ("HOLDER", 0, -1, 3, ["减持计划", "减持股份", "减持进展", "股份减持"]),
    ("PLEDGE", 0, -1, 2, ["股份质押", "质押股份"]),
    ("PLEDGE_RECOVERY", 5, +1, 2, ["解除质押", "解除股份质押"]),
    ("INQUIRY", 1, -1, 2, ["问询函", "关注函", "监管工作函"]),
    ("INQUIRY_REPLY", 4, +1, 2, ["回复问询函", "问询函回复", "回复关注函"]),
    ("MANAGEMENT", 0, -1, 2, ["董事长辞职", "总经理辞职", "高级管理人员辞职"]),
    ("MANAGEMENT", 3, 0, 1, ["聘任总经理", "聘任董事长", "董事会换届", "管理层变更"]),
]

IGNORE_HINTS = ["提示性公告", "更正公告", "补充公告", "独立董事意见", "法律意见书", "审计报告"]


def session():
    s = requests.Session()
    retry = Retry(total=4, connect=4, read=4, backoff_factor=0.8,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "User-Agent": "Mozilla/5.0 pesticide-quant-github-actions",
        "Referer": "https://data.eastmoney.com/",
        "Accept": "application/json,text/plain,*/*",
    })
    return s


def load_universe(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return sorted({r["code"].strip() for r in csv.DictReader(f) if r.get("code", "").strip()})


def ensure_schema(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS announcement_raw(
      source_id TEXT NOT NULL, canonical_code TEXT NOT NULL, source_code TEXT NOT NULL,
      art_code TEXT NOT NULL, notice_date TEXT NOT NULL, title TEXT NOT NULL,
      columns_json TEXT, raw_json TEXT, ingest_ts TEXT,
      PRIMARY KEY(source_id, canonical_code, art_code));
    CREATE TABLE IF NOT EXISTS announcement_event(
      canonical_code TEXT NOT NULL, art_code TEXT NOT NULL, notice_date TEXT NOT NULL,
      effective_date TEXT, title TEXT NOT NULL, category TEXT NOT NULL,
      stage INTEGER NOT NULL, direction INTEGER NOT NULL, hardness REAL NOT NULL,
      novelty REAL NOT NULL, family_key TEXT NOT NULL, matched_keyword TEXT,
      classification_version TEXT NOT NULL,
      PRIMARY KEY(canonical_code, art_code));
    CREATE TABLE IF NOT EXISTS announcement_feature_daily(
      trade_date TEXT NOT NULL, code TEXT NOT NULL,
      long_term_setup_score REAL,
      recent_acceleration_30d REAL,
      recent_acceleration_90d REAL,
      milestone_progress_score REAL,
      commercialization_score REAL,
      earnings_revision_score REAL,
      negative_event_acceleration REAL,
      positive_event_acceleration REAL,
      event_consistency_score REAL,
      information_novelty_score REAL,
      event_chain_confidence REAL,
      price_digestion_score REAL,
      event_excess_ret_since_key REAL,
      information_price_gap REAL,
      days_since_key_inflection REAL,
      announcement_event_count_30d INTEGER,
      announcement_event_count_90d INTEGER,
      announcement_available REAL,
      source_id TEXT NOT NULL,
      PRIMARY KEY(trade_date, code));
    """)
    con.commit()


def parse_notice_date(v):
    s = str(v or "")[:10]
    try:
        return dt.date.fromisoformat(s)
    except Exception:
        return None


def fetch_source_code(s, source_code, start_date):
    out = []
    page = 1
    while True:
        r = s.get(URL, params={
            "sr": "-1", "page_size": 100, "page_index": page,
            "ann_type": "SHA,CYB,SZA,BJA", "client_source": "web",
            "f_node": "0", "s_node": "0", "stock_list": source_code,
        }, timeout=35)
        r.raise_for_status()
        payload = r.json()
        data = payload.get("data") or {}
        rows = data.get("list") or []
        if not rows:
            break
        stop = False
        for row in rows:
            nd = parse_notice_date(row.get("notice_date") or row.get("display_time"))
            if nd and nd < start_date:
                stop = True
                continue
            out.append(row)
        total = int(data.get("total_hits") or data.get("totalHits") or 0)
        pages = max(1, math.ceil(total / 100)) if total else page
        if stop or page >= pages:
            break
        page += 1
        time.sleep(0.03)
    return out


def fetch_code(s, canonical, start_date):
    by = {}
    for src in ALIASES.get(canonical, [canonical]):
        try:
            rows = fetch_source_code(s, src, start_date)
            print("ANN_SOURCE", canonical, src, len(rows))
        except Exception as exc:
            print("ANN_ERR", canonical, src, repr(exc))
            continue
        for row in rows:
            art = str(row.get("art_code") or row.get("artCode") or "").strip()
            if art:
                by[art] = (src, row)
    return [by[k] for k in sorted(by)]


def normalise_title(title):
    x = re.sub(r"\s+", "", title or "")
    x = re.sub(r"^(?:关于)?", "", x)
    x = re.sub(r"20\d{2}年", "", x)
    x = re.sub(r"第[一二三四五六七八九十\d]+次", "", x)
    return x


def classify(title):
    t = normalise_title(title)
    for category, stage, direction, hard, kws in RULES:
        for kw in kws:
            if kw in t:
                # "终止" overrides otherwise positive project/commercial language.
                if any(z in t for z in ["终止", "取消", "未能", "延期", "推迟"]):
                    return "EXECUTION_RISK", max(stage - 2, 0), -1, max(hard, 3), kw
                return category, stage, direction, hard, kw
    return "OTHER", 0, 0, 0.5, None


def family_key(title, category, stage, kw):
    t = normalise_title(title)
    # Keep a compact topic residue so repeated progress announcements do not
    # masquerade as independent information.
    residue = re.sub(r"公告|关于|公司|股份有限公司|进展|情况|提示|说明", "", t)
    residue = re.sub(r"[（(].*?[）)]", "", residue)
    residue = residue[:24]
    return f"{category}|{stage}|{kw or ''}|{residue}"


def next_trade_date(trading_dates, notice_date):
    if notice_date is None or not len(trading_dates):
        return None
    target = pd.Timestamp(notice_date) + pd.Timedelta(days=1)
    i = trading_dates.searchsorted(target, side="left")
    if i >= len(trading_dates):
        return None
    return pd.Timestamp(trading_dates[i]).date().isoformat()


def build_events(raw_rows, market_dates_by_code):
    events = []
    history = {}
    for canonical, source_code, art_code, notice_date, title in sorted(raw_rows, key=lambda x: (x[0], x[3], x[2])):
        cat, stage, direction, hard, kw = classify(title)
        fam = family_key(title, cat, stage, kw)
        key = (canonical, fam)
        nd = pd.Timestamp(notice_date)
        prev = history.get(key, [])
        repeated = sum(1 for d in prev if 0 <= (nd - d).days <= 120)
        novelty = 1.0 / (1.0 + repeated)
        if any(h in title for h in IGNORE_HINTS) and cat == "OTHER":
            novelty *= 0.5
        history.setdefault(key, []).append(nd)
        eff = next_trade_date(market_dates_by_code.get(canonical, pd.DatetimeIndex([])), nd.date())
        events.append({
            "canonical_code": canonical, "art_code": art_code,
            "notice_date": nd.date().isoformat(), "effective_date": eff,
            "title": title, "category": cat, "stage": int(stage),
            "direction": int(direction), "hardness": float(hard),
            "novelty": float(novelty), "family_key": fam,
            "matched_keyword": kw,
            "classification_version": "ANN_CHAIN_TITLE_V1",
        })
    return pd.DataFrame(events)


def active_industry_daily_return(market, membership):
    z = market[["trade_date", "code", "close_qfq"]].copy()
    z["ret1"] = z.groupby("code")["close_qfq"].pct_change(fill_method=None)
    chunks = []
    by = {k: g for k, g in z.groupby("code", sort=False)}
    for code, gm in membership.groupby("code", sort=False):
        g = by.get(code)
        if g is None:
            continue
        for r in gm.itertuples(index=False):
            mask = g["trade_date"].ge(r.in_date)
            if pd.notna(r.out_date):
                mask &= g["trade_date"].le(r.out_date)
            if mask.any():
                chunks.append(g.loc[mask, ["trade_date", "ret1"]])
    if not chunks:
        return pd.Series(dtype=float)
    a = pd.concat(chunks, ignore_index=True)
    return a.groupby("trade_date")["ret1"].mean().sort_index()


def event_weight(x):
    return float(x.direction) * float(x.hardness) * float(x.novelty)


def score_daily(events, market, membership):
    if events.empty or market.empty:
        return pd.DataFrame()
    market = market.sort_values(["code", "trade_date"]).copy()
    membership = membership.copy()
    membership["in_date"] = pd.to_datetime(membership["in_date"])
    membership["out_date"] = pd.to_datetime(membership["out_date"], errors="coerce")
    industry_ret = active_industry_daily_return(market, membership)
    ind_log = np.log1p(industry_ret.clip(lower=-0.999)).fillna(0).cumsum()

    rows = []
    ev_by = {k: g.sort_values("effective_date").copy() for k, g in events[events["effective_date"].notna()].groupby("canonical_code")}
    for code, g in market.groupby("code", sort=False):
        g = g.sort_values("trade_date").copy()
        ev = ev_by.get(code)
        if ev is None or ev.empty:
            continue
        ev["effective_date"] = pd.to_datetime(ev["effective_date"])
        ev["notice_date"] = pd.to_datetime(ev["notice_date"])
        ev["w"] = ev.apply(event_weight, axis=1)
        dates = g["trade_date"].to_numpy()
        close = pd.Series(pd.to_numeric(g["close_qfq"], errors="coerce").to_numpy(), index=g["trade_date"])
        stock_log = np.log(close).replace([np.inf, -np.inf], np.nan).ffill()

        for d in pd.to_datetime(dates):
            hist = ev[ev["effective_date"] <= d]
            if hist.empty:
                continue
            age = (d - hist["effective_date"]).dt.days
            e30 = hist[(age >= 0) & (age <= 30)]
            e90 = hist[(age >= 0) & (age <= 90)]
            old = hist[(age >= 91) & (age <= 365)]
            y365 = hist[(age >= 0) & (age <= 365)]

            def pos_sum(x):
                return float(x.loc[x["w"] > 0, "w"].sum()) if len(x) else 0.0
            def neg_sum(x):
                return float((-x.loc[x["w"] < 0, "w"]).sum()) if len(x) else 0.0

            p30, p90, pold = pos_sum(e30), pos_sum(e90), pos_sum(old)
            n30, n90, nold = neg_sum(e30), neg_sum(e90), neg_sum(old)
            accel30 = p30 - pold * (30.0 / 275.0)
            accel90 = p90 - pold * (90.0 / 275.0)
            neg_accel = n90 - nold * (90.0 / 275.0)
            setup = float(old[(old["category"] == "PROJECT") & (old["stage"] <= 5)].apply(lambda r: max(r["w"], 0), axis=1).sum())
            project = y365[y365["category"] == "PROJECT"]
            milestone = float(project["stage"].max() / 9.0) if len(project) else 0.0
            commercial = float(y365[y365["category"] == "COMMERCIAL"].apply(lambda r: max(r["w"], 0), axis=1).sum())
            earn = float(y365[y365["category"] == "EARNINGS"]["w"].sum())
            pos = pos_sum(e90); neg = neg_sum(e90)
            consistency = (pos - neg) / (pos + neg + 1e-9)
            novelty = float(e90["novelty"].mean()) if len(e90) else 0.0
            hard_events = e90[e90["hardness"] >= 3]
            confidence = min(1.0, (len(hard_events) / 3.0) * (0.5 + 0.5 * novelty))

            # The key inflection is the most recent hard, directional event.
            key = hist[(hist["hardness"] >= 3) & (hist["direction"] != 0)]
            digestion = np.nan; excess = np.nan; days_key = np.nan; info_gap = np.nan
            if len(key):
                kr = key.iloc[-1]
                kd = pd.Timestamp(kr["effective_date"])
                days_key = float((d - kd).days)
                if kd in stock_log.index and d in stock_log.index and kd in ind_log.index and d in ind_log.index:
                    sret = float(np.exp(stock_log.loc[d] - stock_log.loc[kd]) - 1.0)
                    iret = float(np.exp(ind_log.loc[d] - ind_log.loc[kd]) - 1.0)
                    excess = sret - iret
                    aligned = float(kr["direction"]) * excess
                    digestion = float(np.clip(aligned / 0.30, 0.0, 1.5))
                signed_info = math.tanh((pos - neg) / 5.0)
                discount = 1.0 - min(float(digestion) if np.isfinite(digestion) else 0.0, 1.0)
                info_gap = float(signed_info * discount)

            rows.append({
                "trade_date": d.date().isoformat(), "code": code,
                "long_term_setup_score": setup,
                "recent_acceleration_30d": accel30,
                "recent_acceleration_90d": accel90,
                "milestone_progress_score": milestone,
                "commercialization_score": commercial,
                "earnings_revision_score": earn,
                "negative_event_acceleration": neg_accel,
                "positive_event_acceleration": accel90,
                "event_consistency_score": consistency,
                "information_novelty_score": novelty,
                "event_chain_confidence": confidence,
                "price_digestion_score": digestion,
                "event_excess_ret_since_key": excess,
                "information_price_gap": info_gap,
                "days_since_key_inflection": days_key,
                "announcement_event_count_30d": int(len(e30)),
                "announcement_event_count_90d": int(len(e90)),
                "announcement_available": 1.0,
                "source_id": SOURCE_ID,
            })
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--universe-csv", required=True)
    ap.add_argument("--start", default="2019-06-01")
    ap.add_argument("--audit-json")
    ap.add_argument("--event-csv")
    args = ap.parse_args()

    start = dt.date.fromisoformat(args.start)
    codes = load_universe(args.universe_csv)
    s = session(); con = sqlite3.connect(args.db)
    ensure_schema(con)
    now = dt.datetime.now().isoformat(timespec="seconds")
    try:
        market = pd.read_sql_query("""SELECT trade_date,code,close_qfq FROM market_daily
            WHERE close_qfq IS NOT NULL ORDER BY code,trade_date""", con)
        market["trade_date"] = pd.to_datetime(market["trade_date"])
        membership = pd.read_sql_query("SELECT code,in_date,out_date FROM industry_membership_history", con)
        market_dates_by_code = {k: pd.DatetimeIndex(g["trade_date"].sort_values().unique()) for k, g in market.groupby("code")}

        con.execute("DELETE FROM announcement_raw WHERE source_id=?", (SOURCE_ID,))
        con.execute("DELETE FROM announcement_event")
        con.execute("DELETE FROM announcement_feature_daily WHERE source_id=?", (SOURCE_ID,))
        con.execute("""INSERT OR REPLACE INTO source_registry(
          source_id,data_layer,source_name,url,coverage,source_type,reliability,v1_status,notes)
          VALUES(?,?,?,?,?,?,?,?,?)""", (
            SOURCE_ID, "announcement_event", "Eastmoney company announcement list", URL,
            f"pesticide PIT universe from {args.start}", "EASTMONEY_ANNOUNCEMENT", "MEDIUM",
            "FETCHING", "title/list metadata V1; effective next trading day; full-text upgrade required"
        ))
        con.commit()

        compact = []
        raw_n = 0
        for i, code in enumerate(codes, 1):
            rows = fetch_code(s, code, start)
            local = 0
            for src, row in rows:
                art = str(row.get("art_code") or row.get("artCode") or "").strip()
                title = str(row.get("title") or "").strip()
                nd = parse_notice_date(row.get("notice_date") or row.get("display_time"))
                if not art or not title or not nd:
                    continue
                con.execute("""INSERT OR REPLACE INTO announcement_raw(
                  source_id,canonical_code,source_code,art_code,notice_date,title,columns_json,raw_json,ingest_ts)
                  VALUES(?,?,?,?,?,?,?,?,?)""", (
                    SOURCE_ID, code, src, art, nd.isoformat(), title,
                    json.dumps(row.get("columns"), ensure_ascii=False),
                    json.dumps(row, ensure_ascii=False), now,
                ))
                compact.append((code, src, art, nd.isoformat(), title))
                local += 1; raw_n += 1
            con.commit()
            print("ANN", i, len(codes), code, local)
            time.sleep(0.03)

        events = build_events(compact, market_dates_by_code)
        if len(events):
            events.to_sql("announcement_event", con, if_exists="append", index=False)
        features = score_daily(events, market, membership)
        if len(features):
            features.to_sql("announcement_feature_daily", con, if_exists="append", index=False)
        con.commit()

        classified = int((events["category"] != "OTHER").sum()) if len(events) else 0
        directional = int((events["direction"] != 0).sum()) if len(events) else 0
        hard = int((events["hardness"] >= 3).sum()) if len(events) else 0
        summary = {
            "source": SOURCE_ID,
            "target_codes": len(codes), "raw_rows": raw_n,
            "event_rows": int(len(events)), "classified_rows": classified,
            "directional_rows": directional, "hard_milestone_rows": hard,
            "classified_rate": classified / len(events) if len(events) else 0.0,
            "feature_rows": int(len(features)),
            "feature_codes": int(features["code"].nunique()) if len(features) else 0,
            "feature_min": None if not len(features) else str(features["trade_date"].min()),
            "feature_max": None if not len(features) else str(features["trade_date"].max()),
            "pit_rule": "effective next observed trading day after notice_date",
            "classification_version": "ANN_CHAIN_TITLE_V1",
            "limitation": "title/list metadata only; full-text event extraction not yet implemented",
        }
        con.execute("UPDATE source_registry SET v1_status=?,notes=notes||? WHERE source_id=?", (
            "FETCHED_TITLE_V1" if raw_n else "EMPTY",
            "; " + json.dumps(summary, ensure_ascii=False), SOURCE_ID,
        ))
        con.commit()
        if args.event_csv and len(events):
            events.to_csv(args.event_csv, index=False, encoding="utf-8-sig")
        if args.audit_json:
            Path(args.audit_json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if raw_n == 0:
            raise SystemExit(2)
    finally:
        con.close()


if __name__ == "__main__":
    main()
