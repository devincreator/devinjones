#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Four-module PIT pesticide backtest.

Modules
-------
TECH      price/volume structure known on trade_date
FIN       conservative PIT finance, available_date=max(NOTICE_DATE, UPDATE_DATE)
CAPITAL   Eastmoney margin financing observations, exact-date only
INDUSTRY  pesticide-member market cycle + NBS glyphosate price releases

The daily classification metrics are intentionally kept separate from a
non-overlapping 60-trading-day event evaluation.  Daily rows share label windows
and must not be presented as independent trade wins.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

import backtest as base
import backtest_v2 as v2

TECH = base.TECH
FIN = base.FIN
CAPITAL = [
    "margin_balance_chg_5obs",
    "margin_balance_chg_20obs",
    "margin_net_buy_5_to_balance",
    "margin_net_buy_20_to_balance",
    "margin_available",
]
INDUSTRY_MARKET = [
    "industry_ret20_mean",
    "industry_ret60_mean",
    "industry_breadth_ma20",
    "industry_breadth_ma60",
    "industry_ret20_dispersion",
    "industry_amount_z20_mean",
]
INDUSTRY_PRICE = [
    "glyphosate_price",
    "glyphosate_chg_1obs",
    "glyphosate_chg_3obs",
    "glyphosate_chg_6obs",
    "glyphosate_z12obs",
    "glyphosate_available",
]
INDUSTRY = INDUSTRY_MARKET + INDUSTRY_PRICE

MODEL_SETS = {
    "TECH": TECH,
    "TECH_FIN": TECH + FIN,
    "TECH_FIN_CAP": TECH + FIN + CAPITAL,
    "TECH_FIN_INDUSTRY": TECH + FIN + INDUSTRY,
    "ALL": TECH + FIN + CAPITAL + INDUSTRY,
}


def load_capital(con):
    try:
        x = pd.read_sql_query("""SELECT trade_date,code,margin_balance_cny,margin_buy_cny,
          margin_repay_cny,short_balance_cny FROM capital_flow_daily ORDER BY code,trade_date""", con)
    except Exception:
        return pd.DataFrame()
    if not x.empty:
        x["trade_date"] = pd.to_datetime(x["trade_date"])
    return x


def load_cycle(con):
    try:
        x = pd.read_sql_query("""SELECT date,product_id,price FROM industry_product_daily
          WHERE product_id='NBS_GLYPHOSATE_95' ORDER BY date""", con)
    except Exception:
        return pd.DataFrame()
    if not x.empty:
        x["date"] = pd.to_datetime(x["date"])
    return x


def capital_features(cap):
    if cap.empty:
        return cap
    out = []
    for code, g in cap.groupby("code", sort=False):
        g = g.sort_values("trade_date").copy()
        # Do not let a rolling feature bridge a long reporting/eligibility gap.
        seg = g["trade_date"].diff().dt.days.fillna(0).gt(10).cumsum()
        pieces = []
        for _, z in g.groupby(seg, sort=False):
            z = z.copy()
            bal = pd.to_numeric(z["margin_balance_cny"], errors="coerce")
            buy = pd.to_numeric(z["margin_buy_cny"], errors="coerce")
            repay = pd.to_numeric(z["margin_repay_cny"], errors="coerce")
            net = buy - repay
            z["margin_balance_chg_5obs"] = bal.pct_change(5, fill_method=None)
            z["margin_balance_chg_20obs"] = bal.pct_change(20, fill_method=None)
            z["margin_net_buy_5_to_balance"] = net.rolling(5, min_periods=3).sum() / bal.replace(0, np.nan)
            z["margin_net_buy_20_to_balance"] = net.rolling(20, min_periods=10).sum() / bal.replace(0, np.nan)
            z["margin_available"] = 1.0
            pieces.append(z)
        out.append(pd.concat(pieces, ignore_index=True))
    return pd.concat(out, ignore_index=True)


def join_capital(panel, cap):
    panel = panel.copy()
    if cap.empty:
        for c in CAPITAL[:-1]:
            panel[c] = np.nan
        panel["margin_available"] = 0.0
        return panel
    cf = capital_features(cap)
    cols = ["trade_date", "code"] + CAPITAL
    # Exact-date merge is deliberate.  Carrying the last financing observation
    # forward across a long non-eligibility/vendor gap would manufacture data.
    panel = panel.merge(cf[cols], on=["trade_date", "code"], how="left")
    panel["margin_available"] = panel["margin_available"].fillna(0.0)
    return panel


def add_industry_market_cycle(panel):
    panel = panel.copy()
    rows = []
    for d, g in panel.groupby("trade_date", sort=True):
        r20 = pd.to_numeric(g["ret_20d"], errors="coerce")
        r60 = pd.to_numeric(g["ret_60d"], errors="coerce")
        ma20 = pd.to_numeric(g["ma20_gap"], errors="coerce")
        ma60 = pd.to_numeric(g["ma60_gap"], errors="coerce")
        az = pd.to_numeric(g["amount_z20"], errors="coerce")
        rows.append({
            "trade_date": d,
            "industry_ret20_mean": r20.mean(),
            "industry_ret60_mean": r60.mean(),
            "industry_breadth_ma20": (ma20.dropna() > 0).mean() if ma20.notna().any() else np.nan,
            "industry_breadth_ma60": (ma60.dropna() > 0).mean() if ma60.notna().any() else np.nan,
            "industry_ret20_dispersion": r20.std(ddof=0),
            "industry_amount_z20_mean": az.mean(),
        })
    return panel.merge(pd.DataFrame(rows), on="trade_date", how="left")


def cycle_features(cyc):
    if cyc.empty:
        return cyc
    x = cyc.sort_values("date").drop_duplicates("date", keep="last").copy()
    p = pd.to_numeric(x["price"], errors="coerce")
    x["glyphosate_price"] = p
    x["glyphosate_chg_1obs"] = p.pct_change(1, fill_method=None)
    x["glyphosate_chg_3obs"] = p.pct_change(3, fill_method=None)
    x["glyphosate_chg_6obs"] = p.pct_change(6, fill_method=None)
    mu = p.rolling(12, min_periods=6).mean()
    sd = p.rolling(12, min_periods=6).std(ddof=0).replace(0, np.nan)
    x["glyphosate_z12obs"] = (p - mu) / sd
    x["glyphosate_available"] = 1.0
    return x[["date"] + INDUSTRY_PRICE]


def join_cycle(panel, cyc):
    panel = panel.copy().sort_values("trade_date")
    if cyc.empty:
        for c in INDUSTRY_PRICE[:-1]:
            panel[c] = np.nan
        panel["glyphosate_available"] = 0.0
        return panel
    cf = cycle_features(cyc).rename(columns={"date": "cycle_publication_date"})
    z = pd.merge_asof(
        panel.sort_values("trade_date"), cf.sort_values("cycle_publication_date"),
        left_on="trade_date", right_on="cycle_publication_date",
        direction="backward", allow_exact_matches=True,
    )
    leak = z[z["cycle_publication_date"].notna() & (z["cycle_publication_date"] > z["trade_date"])]
    if len(leak):
        raise RuntimeError(f"industry cycle lookahead {len(leak)}")
    z["glyphosate_available"] = z["glyphosate_available"].fillna(0.0)
    return z


def walk_full(panel):
    panel = panel.copy()
    panel["year"] = panel["trade_date"].dt.year
    years = sorted(panel["year"].dropna().unique())
    metrics, preds = [], []
    for target in ["opportunity_label", "risk_label"]:
        valid = panel[panel[target].notna() & panel["label_end_date"].notna()].copy()
        for y in years[3:]:
            test = valid[valid["year"] == y].copy()
            if test.empty:
                continue
            start = test["trade_date"].min()
            train = valid[(valid["trade_date"] < start) & (valid["label_end_date"] < start)].copy()
            if train.empty or train[target].nunique() < 2:
                continue
            for name, cols in MODEL_SETS.items():
                if train[cols].notna().sum().sum() == 0:
                    continue
                p = base.model_prob(train, test, cols, target)
                m = base.metr(test[target], p)
                m.update({
                    "target": target, "model": name, "test_year": int(y),
                    "train_n": int(len(train)), "test_n": int(len(test)),
                    "train_last_label_end": train["label_end_date"].max().date().isoformat(),
                    "test_start": start.date().isoformat(),
                })
                metrics.append(m)
                z = test[[
                    "trade_date", "code", "label_end_date", "fwd_ret_60d", "max_drawdown_60d",
                    "max_upside_60d", "opportunity_label", "risk_label", "margin_available",
                    "glyphosate_available",
                ]].copy()
                z["target"] = target
                z["model"] = name
                z["test_year"] = int(y)
                z["prob"] = p
                z["pred"] = (p >= .5).astype(int)
                preds.append(z)
    return pd.DataFrame(metrics), pd.concat(preds, ignore_index=True) if preds else pd.DataFrame()


def aggregate(preds):
    rows = []
    if preds.empty:
        return pd.DataFrame()
    for (target, model), g in preds.groupby(["target", "model"]):
        m = base.metr(g[target].astype(int), g["prob"])
        m.update({"target": target, "model": model, "years": int(g["test_year"].nunique())})
        rows.append(m)
    return pd.DataFrame(rows)


def nonoverlap_metrics(preds, thresholds=(0.5, 0.6, 0.7)):
    rows = []
    if preds.empty:
        return pd.DataFrame()
    for threshold in thresholds:
        for (target, model), g0 in preds.groupby(["target", "model"]):
            picked = []
            for code, g in g0[g0["prob"] >= threshold].groupby("code", sort=False):
                last_end = pd.Timestamp.min
                for r in g.sort_values("trade_date").itertuples(index=False):
                    if r.trade_date <= last_end:
                        continue
                    picked.append(r)
                    last_end = r.label_end_date
            if not picked:
                rows.append({"target": target, "model": model, "threshold": threshold, "events": 0})
                continue
            z = pd.DataFrame([r._asdict() for r in picked])
            rows.append({
                "target": target, "model": model, "threshold": threshold,
                "events": int(len(z)), "event_hit_rate": float(z[target].astype(float).mean()),
                "avg_fwd_ret_60d": float(z["fwd_ret_60d"].mean()),
                "median_fwd_ret_60d": float(z["fwd_ret_60d"].median()),
                "avg_max_drawdown_60d": float(z["max_drawdown_60d"].mean()),
                "avg_max_upside_60d": float(z["max_upside_60d"].mean()),
                "years": int(z["test_year"].nunique()),
            })
    return pd.DataFrame(rows)


def common_sample(panel):
    # Sensitivity sample where the two externally sourced modules are observed.
    # It is NOT the main production sample because restricting to margin-eligible
    # stocks changes the universe composition.
    fin_any = panel[FIN].notna().any(axis=1)
    return panel[(panel["margin_available"] == 1) & (panel["glyphosate_available"] == 1) & fin_any].copy()


def gate_full(con, m, mem, fin, cap, cyc):
    g = v2.gate_v2(con, m, mem, fin)
    g.update({
        "capital_rows": int(len(cap)),
        "capital_codes": int(cap["code"].nunique()) if not cap.empty else 0,
        "cycle_rows": int(len(cyc)),
        "cycle_min_date": None if cyc.empty else cyc["date"].min().date().isoformat(),
        "cycle_max_date": None if cyc.empty else cyc["date"].max().date().isoformat(),
    })
    g["full_formal_ok"] = bool(g.get("formal_ok") and len(cap) > 0 and len(cyc) >= 30)
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--allow-partial", action="store_true")
    args = ap.parse_args()

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(args.db)
    try:
        m, mem, fin = base.load(con)
        cap, cyc = load_capital(con), load_cycle(con)
        gate = gate_full(con, m, mem, fin, cap, cyc)
        (out / "gate_full.json").write_text(json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(gate, ensure_ascii=False, indent=2))
        if not gate["full_formal_ok"] and not args.allow_partial:
            raise SystemExit(2)
        if m.empty or mem.empty:
            raise SystemExit(3)

        tf = base.tech_features(m)
        feat = base.feature_dates(tf, mem)
        feat = base.join_fin(feat, fin)
        feat = join_capital(feat, cap)
        feat = add_industry_market_cycle(feat)
        feat = join_cycle(feat, cyc)
        panel = base.labels(feat, m)

        metrics, preds = walk_full(panel)
        agg = aggregate(preds)
        events = nonoverlap_metrics(preds)

        common = common_sample(panel)
        cm, cp = walk_full(common) if len(common) else (pd.DataFrame(), pd.DataFrame())
        cagg = aggregate(cp)
        cevents = nonoverlap_metrics(cp)

        panel.to_csv(out / "pit_panel_full.csv", index=False, encoding="utf-8-sig")
        metrics.to_csv(out / "metrics_by_year_full.csv", index=False, encoding="utf-8-sig")
        agg.to_csv(out / "metrics_overall_full.csv", index=False, encoding="utf-8-sig")
        preds.to_csv(out / "predictions_full.csv", index=False, encoding="utf-8-sig")
        events.to_csv(out / "nonoverlap_event_metrics.csv", index=False, encoding="utf-8-sig")
        cm.to_csv(out / "metrics_by_year_common_sample.csv", index=False, encoding="utf-8-sig")
        cagg.to_csv(out / "metrics_overall_common_sample.csv", index=False, encoding="utf-8-sig")
        cevents.to_csv(out / "nonoverlap_event_metrics_common_sample.csv", index=False, encoding="utf-8-sig")

        summary = {
            "status": "FORMAL_FOUR_MODULE" if gate["full_formal_ok"] else "PARTIAL_FOUR_MODULE",
            "gate": gate,
            "label_definition": {
                "horizon_trading_days": 60,
                "opportunity": "fwd_ret_60d>=15% AND max_drawdown_60d>-10%",
                "risk": "max_drawdown_60d<=-15%",
            },
            "panel_rows": int(len(panel)),
            "panel_codes": int(panel["code"].nunique()) if len(panel) else 0,
            "common_sample_rows": int(len(common)),
            "common_sample_codes": int(common["code"].nunique()) if len(common) else 0,
            "daily_classification_note": "daily rows overlap; precision is not an independent trade win rate",
            "nonoverlap_note": "per-stock predicted-positive events are greedily spaced beyond prior 60d label_end_date",
            "aggregate": agg.to_dict("records"),
            "nonoverlap_events": events.to_dict("records"),
            "aggregate_common_sample": cagg.to_dict("records"),
            "nonoverlap_events_common_sample": cevents.to_dict("records"),
        }
        (out / "backtest_full_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        con.close()


if __name__ == "__main__":
    main()
