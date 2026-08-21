#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build PIT glyphosate industry-cycle series from official NBS releases.

Source: 国家统计局《流通领域重要生产资料市场价格变动情况》
Product: 农药（草甘膦，95%原药）

PIT rule: ``industry_product_daily.date`` is the PUBLICATION date, never the
observation period. The model may only see a 旬度 price after NBS published it.

NBS's site-search endpoint is not reliable for historical discovery, so this
loader walks the official ``/sj/zxfb/`` release-index pages. NBS responses can
omit a charset in Content-Type; decoding ``requests.Response.text`` may then
produce mojibake. All pages are therefore decoded from bytes explicitly.
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

INDEX_ROOT = "https://www.stats.gov.cn/sj/zxfb/"
SOURCE_ID = "S019"
PRODUCT_ID = "NBS_GLYPHOSATE_95"
PRODUCT_NAME = "农药（草甘膦，95%原药）"
TITLE_KEY = "流通领域重要生产资料市场价格变动情况"


def session():
    s = requests.Session()
    retry = Retry(
        total=4, connect=4, read=4, backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "User-Agent": "Mozilla/5.0 pesticide-quant-github-actions",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        "Referer": "https://www.stats.gov.cn/",
    })
    return s


def response_text(r):
    """Decode NBS HTML deterministically; charset headers are not always usable."""
    b = r.content
    for enc in ("utf-8", "gb18030"):
        try:
            t = b.decode(enc)
            # A successful codec decode is not enough; require recognizable NBS text.
            if "国家统计局" in t or "数据发布" in t or TITLE_KEY in t:
                return t
        except UnicodeDecodeError:
            pass
    enc = r.apparent_encoding or r.encoding or "utf-8"
    return b.decode(enc, errors="replace")


def parse_period_from_title(title: str):
    m = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(上旬|中旬|下旬).*流通领域重要生产资料市场价格变动情况", title)
    if not m:
        return None
    y, mo, part = int(m.group(1)), int(m.group(2)), m.group(3)
    day = {"上旬": 10, "中旬": 20, "下旬": calendar.monthrange(y, mo)[1]}[part]
    return f"{y}-{mo:02d}-{part}", dt.date(y, mo, day)


def parse_date_any(text: str):
    for pat in [
        r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})",
        r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日",
    ]:
        m = re.search(pat, text or "")
        if m:
            try:
                return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
    return None


def index_url(page: int):
    return urljoin(INDEX_ROOT, "index.html" if page == 0 else f"index_{page}.html")


def discover_releases(s, start: dt.date, end: dt.date, max_pages: int = 120):
    found = {}
    seen_urls = set()
    old_enough_pages = 0
    empty_pages = 0
    for page in range(max_pages):
        u = index_url(page)
        try:
            r = s.get(u, timeout=30)
            if r.status_code == 404:
                break
            r.raise_for_status()
            html = response_text(r)
        except Exception as exc:
            print("NBS_INDEX_ERR", page, u, repr(exc))
            if page == 0:
                raise
            continue

        soup = BeautifulSoup(html, "lxml")
        page_hits = 0
        page_dates = []
        for a in soup.find_all("a", href=True):
            title = " ".join(a.stripped_strings).strip()
            if TITLE_KEY not in title:
                continue
            parsed = parse_period_from_title(title)
            if not parsed:
                continue
            period, period_end = parsed
            article = urljoin(r.url, a.get("href"))
            if "stats.gov.cn" not in article or article in seen_urls:
                continue
            seen_urls.add(article)
            page_hits += 1
            container_text = " ".join((a.parent or a).stripped_strings)
            listed_date = parse_date_any(container_text)
            if listed_date:
                page_dates.append(listed_date)
            if start <= period_end <= end:
                found[period] = {
                    "period": period, "period_end": period_end, "title": title,
                    "url": article, "listed_date": listed_date,
                }
        print("NBS_INDEX", page, "hits", page_hits, "total_periods", len(found),
              "bytes", len(r.content), "encoding", r.encoding, "apparent", r.apparent_encoding)
        empty_pages = empty_pages + 1 if page_hits == 0 else 0
        if page_dates and min(page_dates) < start - dt.timedelta(days=45):
            old_enough_pages += 1
        elif page_dates:
            old_enough_pages = 0
        if old_enough_pages >= 2:
            break
        if empty_pages >= 12 and page > 11:
            break
        time.sleep(0.04)
    return [found[k] for k in sorted(found)]


def parse_publish_date(html):
    soup = BeautifulSoup(html, "lxml")
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or meta.get("property") or "").lower()
        val = meta.get("content") or ""
        if any(k in name for k in ["pub", "publish", "date", "time"]):
            d = parse_date_any(val)
            if d:
                return d
    text = " ".join(soup.stripped_strings)
    for chunk in [text[:3500], text]:
        d = parse_date_any(chunk)
        if d:
            return d
    return None


def _cell_text(v):
    if isinstance(v, tuple):
        return " ".join(str(x) for x in v if str(x) != "nan")
    return "" if pd.isna(v) else str(v).strip()


def parse_price(html):
    try:
        tables = pd.read_html(StringIO(html))
    except Exception:
        tables = []
    for df in tables:
        for _, row in df.iterrows():
            vals = [_cell_text(x) for x in row.tolist()]
            if "草甘膦" not in " ".join(vals):
                continue
            unit_idx = next((i for i, v in enumerate(vals) if v.strip() == "吨"), None)
            scan = vals[unit_idx + 1:] if unit_idx is not None else vals
            for v in scan:
                z = re.sub(r"[,\s]", "", v)
                if re.fullmatch(r"-?\d+(?:\.\d+)?", z):
                    x = float(z)
                    if x > 1000:
                        return x
    text = " ".join(BeautifulSoup(html, "lxml").stripped_strings)
    m = re.search(r"农药[（(]草甘膦[^）)]*[）)]\s*吨\s*([0-9,]+(?:\.\d+)?)", text)
    return float(m.group(1).replace(",", "")) if m else None


def expected_periods(start, end):
    out = set()
    cur = dt.date(start.year, start.month, 1)
    while cur <= end:
        for part, day in [("上旬", 10), ("中旬", 20), ("下旬", calendar.monthrange(cur.year, cur.month)[1])]:
            pend = dt.date(cur.year, cur.month, day)
            if start <= pend <= end:
                out.add(f"{cur.year}-{cur.month:02d}-{part}")
        cur = dt.date(cur.year + (cur.month == 12), 1 if cur.month == 12 else cur.month + 1, 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--start", default="2020-06-01")
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--audit-json", default="work/industry_cycle_nbs_audit.json")
    ap.add_argument("--max-index-pages", type=int, default=120)
    args = ap.parse_args()
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    s = session()
    con = sqlite3.connect(args.db)
    now = dt.datetime.now().isoformat(timespec="seconds")
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS industry_product_observation(
            product_id TEXT NOT NULL,observation_period TEXT NOT NULL,period_end TEXT,
            publication_date TEXT,price REAL,unit TEXT,source_url TEXT,status TEXT,ingest_ts TEXT,
            PRIMARY KEY(product_id,observation_period))""")
        con.execute("""INSERT OR REPLACE INTO source_registry(
            source_id,data_layer,source_name,url,coverage,source_type,reliability,v1_status,notes)
            VALUES(?,?,?,?,?,?,?,?,?)""", (
            SOURCE_ID, "industry_product_daily", "国家统计局流通领域草甘膦95%原药价格",
            INDEX_ROOT, "2020-06-01 onward, 旬度 releases", "NBS_OFFICIAL_WEB", "HIGH", "FETCHING",
            "PIT date uses publication_date; discovery via official /sj/zxfb/ release indexes",
        ))
        con.execute("DELETE FROM industry_product_daily WHERE source_id=?", (SOURCE_ID,))
        con.execute("DELETE FROM industry_product_observation WHERE product_id=?", (PRODUCT_ID,))
        con.commit()

        releases = discover_releases(s, start, end, args.max_index_pages)
        print("NBS_DISCOVERED", len(releases))
        ok, miss = [], []
        for i, rec in enumerate(releases, 1):
            period, period_end, url = rec["period"], rec["period_end"], rec["url"]
            try:
                r = s.get(url, timeout=30)
                r.raise_for_status()
                html = response_text(r)
                pub = parse_publish_date(html) or rec.get("listed_date")
                price = parse_price(html)
            except Exception as exc:
                print("NBS_PAGE_ERR", period, url, repr(exc))
                miss.append({"period": period, "reason": "page_error", "url": url})
                continue
            if not pub or price is None:
                print("NBS_PARSE_MISS", period, "pub", pub, "price", price, url)
                con.execute("INSERT OR REPLACE INTO industry_product_observation VALUES(?,?,?,?,?,?,?,?,?)",
                            (PRODUCT_ID, period, period_end.isoformat(), pub.isoformat() if pub else None,
                             price, "元/吨", url, "PARSE_MISS", now))
                con.commit()
                miss.append({"period": period, "reason": "parse_miss", "url": url})
                continue
            if pub < period_end - dt.timedelta(days=5) or pub > period_end + dt.timedelta(days=45):
                print("NBS_DATE_REJECT", period, period_end, pub, url)
                miss.append({"period": period, "reason": "bad_publish_date", "url": url})
                continue
            con.execute("INSERT OR REPLACE INTO industry_product_observation VALUES(?,?,?,?,?,?,?,?,?)",
                        (PRODUCT_ID, period, period_end.isoformat(), pub.isoformat(), price,
                         "元/吨", url, "OK", now))
            con.execute("""INSERT OR REPLACE INTO industry_product_daily(
                date,product_id,product_name,category,price,unit,region,frequency,source_id,ingest_ts)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (pub.isoformat(), PRODUCT_ID, PRODUCT_NAME, "农药原药", price,
                         "元/吨", "全国", "旬度", SOURCE_ID, now))
            con.commit()
            ok.append({"period": period, "publication_date": pub.isoformat(), "price": price, "url": url})
            print("NBS_OK", i, len(releases), period, pub, price)
            time.sleep(0.04)

        expected = expected_periods(start, end)
        got = {x["period"] for x in ok}
        for p in sorted(expected - got):
            if not any(x.get("period") == p for x in miss):
                miss.append({"period": p, "reason": "not_discovered"})
        mn, mx = con.execute(
            "SELECT MIN(date),MAX(date) FROM industry_product_daily WHERE source_id=?", (SOURCE_ID,)
        ).fetchone()
        coverage_ratio = len(got) / len(expected) if expected else 0.0
        audit = {
            "rows": len(ok), "expected_periods": len(expected), "missing": len(expected - got),
            "coverage_ratio": coverage_ratio, "min_publication_date": mn,
            "max_publication_date": mx, "missing_samples": miss[:40],
        }
        Path(args.audit_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.audit_json).write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        status = "FETCHED" if len(ok) >= 30 and coverage_ratio >= 0.70 else ("PARTIAL" if ok else "FAILED")
        con.execute("UPDATE source_registry SET v1_status=?,notes=notes||? WHERE source_id=?",
                    (status, f"; rows={len(ok)} expected={len(expected)} coverage={coverage_ratio:.3f} min={mn} max={mx}", SOURCE_ID))
        con.commit()
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        if status != "FETCHED":
            raise SystemExit(2)
    finally:
        con.close()


if __name__ == "__main__":
    main()
