#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fill missing market dates for the official pesticide universe with Sina.

Existing AStock rows are kept. Sina only fills dates where qfq OHLC is absent.
For SH/SZ, the canonical symbol is used directly. For BJ renamed securities,
old and new symbols are stitched into the canonical code.

Sina does not supply turnover or traded amount on this endpoint, so those
columns remain NULL on filled rows. The backtest's price/volatility features
remain available; liquidity features become missing and are imputed by the
model rather than fabricated.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from datetime import date

import pandas as pd

from cnequity.adapters.sina.bars import fetch_daily_bars_sina
from cnequity.adapters.sina.adj_factors import fetch_adj_factor_series

SOURCE_ID = "S017"
SAMPLE_START = date(2020, 6, 1)
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


def exchange(code: str) -> str:
    if code.startswith(("60", "68")):
        return "SH"
    if code.startswith(("92", "43", "83", "87")):
        return "BJ"
    return "SZ"


def factor_series(symbol: str, fallback_symbol: str | None = None) -> pd.DataFrame:
    errors = []
    for sym in [symbol, fallback_symbol]:
        if not sym:
            continue
        try:
            f = fetch_adj_factor_series(sym, "qfq").to_pandas()
            if not f.empty:
                f["trade_date"] = pd.to_datetime(f["trade_date"])
                print("FACTOR", symbol, "USING", sym, len(f), f["trade_date"].min().date(), f["trade_date"].max().date())
                return f[["trade_date", "factor"]].sort_values("trade_date")
        except Exception as e:
            errors.append((sym, repr(e)))
    print("FACTOR_EMPTY", symbol, errors)
    return pd.DataFrame(columns=["trade_date", "factor"])


def adjusted_bars(symbol: str, canonical_factor_symbol: str | None = None) -> pd.DataFrame:
    bars = fetch_daily_bars_sina(symbol, start=SAMPLE_START).to_pandas()
    if bars.empty:
        print("SINA_EMPTY", symbol)
        return bars
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    fac = factor_series(symbol, canonical_factor_symbol)
    if fac.empty:
        # Do not assume raw==qfq. Keeping this source out is safer than injecting
        # corporate-action jumps into the model.
        print("SKIP_NO_QFQ_FACTOR", symbol, len(bars))
        return pd.DataFrame()
    x = pd.merge_asof(
        bars.sort_values("trade_date"), fac, on="trade_date",
        direction="backward", allow_exact_matches=True,
    )
    x["factor"] = pd.to_numeric(x["factor"], errors="coerce").fillna(1.0)
    for c in ["open", "high", "low", "close"]:
        x[c + "_qfq"] = pd.to_numeric(x[c], errors="coerce") * x["factor"]
    return x


def fetch_code(code: str) -> pd.DataFrame:
    if code in ALIASES:
        frames = []
        canonical_symbol = f"{code}.BJ"
        for symbol, start_s, end_s in ALIASES[code]:
            try:
                x = adjusted_bars(symbol, canonical_symbol if symbol != canonical_symbol else None)
            except Exception as e:
                print("SINA_ERR", code, symbol, repr(e))
                continue
            if x.empty:
                continue
            if start_s:
                x = x[x["trade_date"] >= pd.Timestamp(start_s)]
            if end_s:
                x = x[x["trade_date"] <= pd.Timestamp(end_s)]
            if not x.empty:
                x = x.copy(); x["source_symbol"] = symbol; frames.append(x)
        if not frames:
            return pd.DataFrame()
        z = pd.concat(frames, ignore_index=True)
        z = z.sort_values(["trade_date", "source_symbol"]).drop_duplicates("trade_date", keep="last")
        z["code"] = code
        return z.sort_values("trade_date")

    sym = f"{code}.{exchange(code)}"
    try:
        z = adjusted_bars(sym)
    except Exception as e:
        print("SINA_ERR", code, sym, repr(e))
        return pd.DataFrame()
    if not z.empty:
        z = z.copy(); z["code"] = code
    return z


def insert_missing(con: sqlite3.Connection, code: str, x: pd.DataFrame) -> int:
    if x.empty:
        return 0
    now = dt.datetime.now().isoformat(timespec="seconds")
    existing = {r[0] for r in con.execute(
        "SELECT trade_date FROM market_daily WHERE code=? AND close_qfq IS NOT NULL", (code,)
    )}
    n = 0
    prev_raw = None
    for r in x.sort_values("trade_date").itertuples(index=False):
        d = pd.Timestamp(r.trade_date).date().isoformat()
        raw_close = float(r.close)
        if d in existing:
            prev_raw = raw_close
            continue
        pct = None if prev_raw in (None, 0) else (raw_close / prev_raw - 1.0) * 100.0
        con.execute(
            """INSERT INTO market_daily(
            trade_date,code,open_raw,high_raw,low_raw,close_raw,prev_close_raw,pct_chg_pct,
            volume_shares,amount_cny,turnover_pct,adj_factor,close_qfq,is_trade,is_st,limit_status,
            source_id,ingest_ts,open_qfq,high_qfq,low_qfq,amplitude_pct,change_amt)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trade_date,code) DO NOTHING""",
            (d, code, float(r.open), float(r.high), float(r.low), raw_close, prev_raw, pct,
             float(r.volume), None, None, float(r.factor), float(r.close_qfq), 1, None, None,
             SOURCE_ID, now, float(r.open_qfq), float(r.high_qfq), float(r.low_qfq), None, None),
        )
        if con.total_changes:
            n += 1
        prev_raw = raw_close
    return n


def target_codes(con: sqlite3.Connection) -> list[str]:
    # Only intervals that could overlap the sample need market repairs.
    return [r[0] for r in con.execute(
        """SELECT DISTINCT code FROM industry_membership_history
           WHERE out_date IS NULL OR out_date>=? ORDER BY code""",
        (SAMPLE_START.isoformat(),),
    )]


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--db", required=True); args = ap.parse_args()
    con = sqlite3.connect(args.db)
    try:
        con.execute(
            """INSERT OR REPLACE INTO source_registry(
            source_id,data_layer,source_name,url,coverage,source_type,reliability,v1_status,notes)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (SOURCE_ID,"market_daily","Sina qfq gap fill via CNEquity",
             "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
             "official pesticide intervals overlapping 2020-06-01+","HTTP_SINA","MEDIUM","GAP_FILL",
             "fills only missing qfq dates; amount/turnover remain NULL; existing AStock rows preserved"),
        )
        codes = target_codes(con)
        total = 0; done = 0
        for i, code in enumerate(codes, 1):
            x = fetch_code(code)
            n = insert_missing(con, code, x)
            con.commit()
            print("GAP_FILL", i, len(codes), code, n)
            if n: done += 1; total += n
        all_codes = con.execute("SELECT COUNT(DISTINCT code) FROM market_daily WHERE close_qfq IS NOT NULL").fetchone()[0]
        all_rows = con.execute("SELECT COUNT(*) FROM market_daily WHERE close_qfq IS NOT NULL").fetchone()[0]
        mn,mx = con.execute("SELECT MIN(trade_date),MAX(trade_date) FROM market_daily WHERE close_qfq IS NOT NULL").fetchone()
        con.execute(
            """UPDATE ingestion_job SET loaded_entities=?,loaded_rows=?,min_date=?,max_date=?,last_attempt_ts=?,
               note=COALESCE(note,'')||? WHERE job_id='J001'""",
            (all_codes,all_rows,mn,mx,dt.datetime.now().isoformat(timespec="seconds"),
             f"; Sina qfq gap-fill rows={total} codes={done}"),
        )
        con.commit()
        print({"target_codes":len(codes),"gap_fill_codes":done,"gap_fill_rows":total,"market_codes":all_codes,"market_rows":all_rows,"min":mn,"max":mx})
    finally:
        con.close()


if __name__ == "__main__": main()
