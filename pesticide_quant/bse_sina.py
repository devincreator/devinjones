#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fill Beijing Stock Exchange market history from Sina via CNEquity.

Canonical aliases:
- 920819 <- 833819 through 2025-05-05, then 920819
- 920866 <- 870866 through 2025-10-08, then 920866

Raw Sina OHLC is adjusted with Sina qfq factors using the same backward as-of
alignment documented by CNEquity. Amount/turnover remain NULL because Sina does
not provide them on this endpoint.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from datetime import date

import pandas as pd

from cnequity.adapters.sina.bars import fetch_daily_bars_sina
from cnequity.adapters.sina.adj_factors import fetch_adj_factor_series

SOURCE_ID = "S016"
START = date(2020, 6, 1)
ALIASES = {
    "920819": [
        ("833819.BJ", None, "2025-05-05"),
        ("920819.BJ", "2025-05-06", None),
    ],
    "920866": [
        ("870866.BJ", None, "2025-10-08"),
        ("920866.BJ", "2025-10-09", None),
    ],
}


def fetch_qfq(symbol: str) -> pd.DataFrame:
    bars = fetch_daily_bars_sina(symbol, start=START).to_pandas()
    if bars.empty:
        return bars
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    factors = fetch_adj_factor_series(symbol, "qfq").to_pandas()
    if factors.empty:
        bars["factor"] = 1.0
    else:
        factors["trade_date"] = pd.to_datetime(factors["trade_date"])
        factors = factors[["trade_date", "factor"]].sort_values("trade_date")
        bars = pd.merge_asof(
            bars.sort_values("trade_date"),
            factors,
            on="trade_date",
            direction="backward",
        )
        bars["factor"] = bars["factor"].fillna(1.0)
    for c in ["open", "high", "low", "close"]:
        bars[c + "_qfq"] = pd.to_numeric(bars[c], errors="coerce") * pd.to_numeric(bars["factor"], errors="coerce")
    return bars


def fetch_canonical(code: str) -> pd.DataFrame:
    frames = []
    for symbol, start_s, end_s in ALIASES[code]:
        try:
            x = fetch_qfq(symbol)
        except Exception as e:
            print("BSE_FETCH_ERR", code, symbol, repr(e))
            continue
        if x.empty:
            print("BSE_EMPTY", code, symbol)
            continue
        if start_s:
            x = x[x["trade_date"] >= pd.Timestamp(start_s)]
        if end_s:
            x = x[x["trade_date"] <= pd.Timestamp(end_s)]
        if not x.empty:
            x = x.copy()
            x["source_symbol"] = symbol
            frames.append(x)
            print("BSE_SOURCE", code, symbol, len(x), x["trade_date"].min().date(), x["trade_date"].max().date())
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values(["trade_date", "source_symbol"]).drop_duplicates("trade_date", keep="last")
    out["code"] = code
    return out.sort_values("trade_date")


def write(con: sqlite3.Connection, code: str, x: pd.DataFrame) -> int:
    if x.empty:
        return 0
    now = dt.datetime.now().isoformat(timespec="seconds")
    prev = None
    n = 0
    for r in x.itertuples(index=False):
        close_raw = float(r.close)
        pct = None if prev in (None, 0) else (close_raw / prev - 1.0) * 100.0
        vals = (
            pd.Timestamp(r.trade_date).date().isoformat(), code,
            float(r.open), float(r.high), float(r.low), close_raw, prev,
            pct, float(r.volume), None, None, float(r.factor), float(r.close_qfq),
            1, None, None, SOURCE_ID, now,
            float(r.open_qfq), float(r.high_qfq), float(r.low_qfq), None, None,
        )
        con.execute(
            """INSERT INTO market_daily(
            trade_date,code,open_raw,high_raw,low_raw,close_raw,prev_close_raw,pct_chg_pct,
            volume_shares,amount_cny,turnover_pct,adj_factor,close_qfq,is_trade,is_st,limit_status,
            source_id,ingest_ts,open_qfq,high_qfq,low_qfq,amplitude_pct,change_amt)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trade_date,code) DO UPDATE SET
              open_raw=excluded.open_raw,high_raw=excluded.high_raw,low_raw=excluded.low_raw,
              close_raw=excluded.close_raw,prev_close_raw=excluded.prev_close_raw,
              pct_chg_pct=excluded.pct_chg_pct,volume_shares=excluded.volume_shares,
              adj_factor=excluded.adj_factor,close_qfq=excluded.close_qfq,
              open_qfq=excluded.open_qfq,high_qfq=excluded.high_qfq,low_qfq=excluded.low_qfq,
              source_id=excluded.source_id,ingest_ts=excluded.ingest_ts""",
            vals,
        )
        prev = close_raw
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    try:
        con.execute(
            """INSERT OR REPLACE INTO source_registry(
            source_id,data_layer,source_name,url,coverage,source_type,reliability,v1_status,notes)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (SOURCE_ID, "market_daily", "Sina BSE bars + Sina qfq factors via CNEquity",
             "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
             "920819/920866 plus historical aliases", "HTTP_SINA", "MEDIUM", "FALLBACK",
             "amount/turnover unavailable; qfq factors aligned backward as sparse step function"),
        )
        total = 0
        loaded = []
        for code in ALIASES:
            x = fetch_canonical(code)
            n = write(con, code, x)
            con.commit()
            print("BSE_CANONICAL", code, n)
            if n:
                loaded.append(code)
                total += n

        all_codes = con.execute("SELECT COUNT(DISTINCT code) FROM market_daily WHERE close_qfq IS NOT NULL").fetchone()[0]
        all_rows = con.execute("SELECT COUNT(*) FROM market_daily WHERE close_qfq IS NOT NULL").fetchone()[0]
        mn, mx = con.execute("SELECT MIN(trade_date),MAX(trade_date) FROM market_daily WHERE close_qfq IS NOT NULL").fetchone()
        con.execute(
            """UPDATE ingestion_job SET loaded_entities=?,loaded_rows=?,min_date=?,max_date=?,last_attempt_ts=?,
            note=COALESCE(note,'') || ? WHERE job_id='J001'""",
            (all_codes, all_rows, mn, mx, dt.datetime.now().isoformat(timespec="seconds"),
             f"; Sina BSE fallback loaded={loaded}"),
        )
        con.commit()
        print({"bse_loaded": loaded, "bse_rows": total, "market_codes_total": all_codes, "market_rows_total": all_rows})
        if set(loaded) != set(ALIASES):
            raise SystemExit(2)
    finally:
        con.close()


if __name__ == "__main__":
    main()
