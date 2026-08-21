#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Incremental PIT test: four-module baseline vs announcement intelligence.

This file does NOT declare the announcement layer final.  It measures whether
ANN_CHAIN_TITLE_V1 adds OOS information before full-text extraction is built.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

import backtest as base
import backtest_full as full

ANN = [
    "long_term_setup_score",
    "recent_acceleration_30d",
    "recent_acceleration_90d",
    "milestone_progress_score",
    "commercialization_score",
    "earnings_revision_score",
    "negative_event_acceleration",
    "positive_event_acceleration",
    "event_consistency_score",
    "information_novelty_score",
    "event_chain_confidence",
    "price_digestion_score",
    "event_excess_ret_since_key",
    "information_price_gap",
    "days_since_key_inflection",
    "announcement_event_count_30d",
    "announcement_event_count_90d",
    "announcement_available",
]

SETS = {
    "TECH_FIN": base.TECH + base.FIN,
    "ALL4": full.MODEL_SETS["ALL"],
    "ANN": ANN,
    "TECH_FIN_ANN": base.TECH + base.FIN + ANN,
    "ALL4_ANN": full.MODEL_SETS["ALL"] + ANN,
}


def load_ann(con):
    try:
        x = pd.read_sql_query("SELECT * FROM announcement_feature_daily ORDER BY code,trade_date", con)
    except Exception:
        return pd.DataFrame()
    if not x.empty:
        x["trade_date"] = pd.to_datetime(x["trade_date"])
    return x


def join_ann(panel, ann):
    p = panel.copy()
    if ann.empty:
        for c in ANN:
            p[c] = 0.0 if c == "announcement_available" else np.nan
        return p
    cols = ["trade_date", "code"] + ANN
    p = p.merge(ann[cols], on=["trade_date", "code"], how="left")
    p["announcement_available"] = p["announcement_available"].fillna(0.0)
    return p


def walk(panel):
    panel = panel.copy(); panel["year"] = panel["trade_date"].dt.year
    years = sorted(panel["year"].dropna().unique())
    metrics, preds = [], []
    for target in ["opportunity_label", "risk_label"]:
        valid = panel[panel[target].notna() & panel["label_end_date"].notna()].copy()
        for y in years[3:]:
            test = valid[valid["year"] == y].copy()
            if test.empty: continue
            start = test["trade_date"].min()
            train = valid[(valid["trade_date"] < start) & (valid["label_end_date"] < start)].copy()
            if train.empty or train[target].nunique() < 2: continue
            for name, cols in SETS.items():
                if train[cols].notna().sum().sum() == 0: continue
                p = base.model_prob(train, test, cols, target)
                m = base.metr(test[target], p)
                m.update({"target": target, "model": name, "test_year": int(y),
                          "train_n": int(len(train)), "test_n": int(len(test)),
                          "train_last_label_end": train["label_end_date"].max().date().isoformat(),
                          "test_start": start.date().isoformat()})
                metrics.append(m)
                z = test[["trade_date","code","label_end_date","fwd_ret_60d","max_drawdown_60d",
                          "max_upside_60d","opportunity_label","risk_label"]].copy()
                z["target"] = target; z["model"] = name; z["test_year"] = int(y)
                z["prob"] = p; z["pred"] = (p >= .5).astype(int)
                preds.append(z)
    return pd.DataFrame(metrics), pd.concat(preds, ignore_index=True) if preds else pd.DataFrame()


def aggregate(preds):
    rows=[]
    if preds.empty: return pd.DataFrame()
    for (target, model), g in preds.groupby(["target","model"]):
        m=base.metr(g[target].astype(int),g["prob"])
        m.update({"target":target,"model":model,"years":int(g["test_year"].nunique())})
        rows.append(m)
    return pd.DataFrame(rows)


def delta_table(agg):
    rows=[]
    if agg.empty: return pd.DataFrame()
    for target, g in agg.groupby("target"):
        base4 = g[g["model"]=="ALL4"]
        combo = g[g["model"]=="ALL4_ANN"]
        if base4.empty or combo.empty: continue
        a=base4.iloc[0]; b=combo.iloc[0]
        rows.append({
            "target":target,
            "precision_all4":a["precision_win_rate"], "precision_all4_ann":b["precision_win_rate"],
            "precision_delta":b["precision_win_rate"]-a["precision_win_rate"],
            "auc_all4":a["auc"], "auc_all4_ann":b["auc"],
            "auc_delta":None if pd.isna(a["auc"]) or pd.isna(b["auc"]) else b["auc"]-a["auc"],
            "recall_delta":b["recall"]-a["recall"],
        })
    return pd.DataFrame(rows)


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--db",required=True); ap.add_argument("--outdir",required=True); ap.add_argument("--allow-partial",action="store_true"); args=ap.parse_args()
    out=Path(args.outdir); out.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(args.db)
    try:
        m,mem,fin=base.load(con); cap=full.load_capital(con); cyc=full.load_cycle(con); ann=load_ann(con)
        gate=full.gate_full(con,m,mem,fin,cap,cyc)
        gate.update({"announcement_rows":int(len(ann)), "announcement_codes":int(ann["code"].nunique()) if len(ann) else 0,
                     "announcement_incremental_ok":bool(len(ann)>0)})
        (out/"gate_announcement.json").write_text(json.dumps(gate,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(gate,ensure_ascii=False,indent=2))
        if (not gate.get("full_formal_ok") or not len(ann)) and not args.allow_partial: raise SystemExit(2)
        if m.empty or mem.empty: raise SystemExit(3)

        tf=base.tech_features(m); feat=base.feature_dates(tf,mem); feat=base.join_fin(feat,fin)
        feat=full.join_capital(feat,cap); feat=full.add_industry_market_cycle(feat); feat=full.join_cycle(feat,cyc)
        feat=join_ann(feat,ann); panel=base.labels(feat,m)
        metrics,preds=walk(panel); agg=aggregate(preds); events=full.nonoverlap_metrics(preds); delta=delta_table(agg)

        panel.to_csv(out/"pit_panel_announcement.csv",index=False,encoding="utf-8-sig")
        metrics.to_csv(out/"metrics_by_year_announcement.csv",index=False,encoding="utf-8-sig")
        agg.to_csv(out/"metrics_overall_announcement.csv",index=False,encoding="utf-8-sig")
        events.to_csv(out/"nonoverlap_event_metrics_announcement.csv",index=False,encoding="utf-8-sig")
        delta.to_csv(out/"announcement_incremental_delta.csv",index=False,encoding="utf-8-sig")
        summary={"status":"ANNOUNCEMENT_INCREMENTAL_TEST", "gate":gate, "panel_rows":int(len(panel)),
                 "panel_codes":int(panel["code"].nunique()), "aggregate":agg.to_dict("records"),
                 "incremental_delta":delta.to_dict("records"),
                 "warning":"ANN_CHAIN_TITLE_V1 is title/list metadata only; a positive delta is evidence to build full-text V2, not a final production claim."}
        (out/"backtest_announcement_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(summary,ensure_ascii=False,indent=2))
    finally:
        con.close()

if __name__=="__main__": main()
