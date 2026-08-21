#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a PIT glyphosate cycle series from official NBS releases.

Source: 国家统计局《流通领域重要生产资料市场价格变动情况》
Product: 农药（草甘膦，95%原药）

PIT contract
------------
The database date is the PUBLICATION date, never the ten-day observation
period.  The feature is therefore unavailable before NBS published the page.

Historical discovery uses the official ``/sj/zxfb/`` release-index archive.
Some NBS article URLs also have the ``/sj/zxfbhjd/`` canonical form; both are
tried when necessary.  NBS frequently labels UTF-8 HTML as ISO-8859-1, so the
response body is decoded from bytes explicitly rather than trusting headers.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urljoin

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
    b = r.content
    for enc in ("utf-8", "gb18030"):
        try:
            t = b.decode(enc)
            if "国家统计局" in t or "数据发布" in t or TITLE_KEY in t or "草甘膦" in t:
                return t
        except UnicodeDecodeError:
            pass
    enc = r.apparent_encoding or r.encoding or "utf-8"
    return b.decode(enc, errors="replace")


def parse_period_from_title(title: str):
    m = re.search(
        r"(20\d{2})年\s*(\d{1,2})月\s*(上旬|中旬|下旬).*流通领域重要生产资料市场价格变动情况",
        title,
    )
    if not m:
        return None
    y, mo, part = int(m.group(1)), int(m.group(2)), m.group(3)
    day = {"上旬": 10, "中旬": 20, "下旬": calendar.monthrange(y, mo)[1]}[part]
    return f"{y}-{mo:02d}-{part}", dt.date(y, mo, day)


def parse_date_any(text: str):
    for pat in (
        r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})",
        r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日",
    ):
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
        for a in soup.find_all("a", href=True):
            title = " ".join(a.stripped_strings).strip()
            if TITLE_KEY not in title:
                continue
            parsed = parse_period_from_title(title)
            if not parsed:
                continue
            period, period_end = parsed
            article = urljoin(r.url, a.get("href"))
            if "stats.gov.cn" not in article:
                continue
            key = (period, article)
            if key in seen_urls:
                continue
            seen_urls.add(key)
            page_hits += 1
            listed_date = parse_date_any(" ".join((a.parent or a).stripped_strings))
            if start <= period_end <= end:
                found[period] = {
                    "period": period,
                    "period_end": period_end,
                    "title": title,
                    "url": article,
                    "listed_date": listed_date,
                }
        print(
            "NBS_INDEX", page, "hits", page_hits, "total_periods", len(found),
            "bytes", len(r.content), "encoding", r.encoding, "apparent", r.apparent_encoding,
        )
        empty_pages = empty_pages + 1 if page_hits == 0 else 0
        # Older NBS index pages use a different archive layout. Stop after a
        # sustained no-hit zone rather than treating it as proof the product
        # did not exist earlier.
        if empty_pages >= 12 and page > 11:
            break
        time.sleep(0.03)
    return [found[k] for k in sorted(found)]


def parse_publish_date(html):
    soup = BeautifulSoup(html, "lxml")
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or meta.get("property") or "").lower()
        val = meta.get("content") or ""
        if any(k in name for k in ("pub", "publish", "date", "time")):
            d = parse_date_any(val)
            if d:
                return d
    text = " ".join(soup.stripped_strings)
    return parse_date_any(text[:5000]) or parse_date_any(text)


def numeric_candidates(text):
    vals = []
    for raw in re.findall(r"(?<!\d)-?\d[\d,]*(?:\.\d+)?", text or ""):
        try:
            x = float(raw.replace(",", ""))
        except ValueError:
            continue
        if 1000 < x < 500000:
            vals.append(x)
    return vals


def parse_price(html):
    """Extract the current-price cell from the glyphosate row.

    NBS has used several table HTML templates.  Locating the text node first
    and then walking to its table row is more stable than relying on pandas'
    inferred column structure.
    """
    soup = BeautifulSoup(html, "lxml")

    for node in soup.find_all(string=re.compile("草甘膦")):
        tr = node.find_parent("tr")
        if tr is not None:
            txt = " ".join(tr.stripped_strings)
            vals = numeric_candidates(txt)
            if vals:
                return vals[0]
        parent = node.parent
        for _ in range(5):
            if parent is None:
                break
            txt = " ".join(parent.stripped_strings)
            if "草甘膦" in txt:
                vals = numeric_candidates(txt)
                if vals:
                    return vals[0]
            parent = parent.parent

    text = " ".join(soup.stripped_strings)
    pos = text.find("草甘膦")
    if pos >= 0:
        vals = numeric_candidates(text[pos:pos + 500])
        if vals:
            return vals[0]

    # Last fallback for templates that separate characters with unusual spaces.
    compact = re.sub(r"\s+", "", text)
    pos = compact.find("草甘膦")
    if pos >= 0:
        vals = numeric_candidates(compact[pos:pos + 400])
        if vals:
            return vals[0]
    return None


def article_variants(url):
    out = [url]
    if "/sj/zxfb/" in url:
        out.append(url.replace("/sj/zxfb/", "/sj/zxfbhjd/"))
    elif "/sj/zxfbhjd/" in url:
        out.append(url.replace("/sj/zxfbhjd/", "/sj/zxfb/"))
    return list(dict.fromkeys(out))


def fetch_article(s, url):
    failures = []
    best = None
    for candidate in article_variants(url):
        try:
            r = s.get(candidate, timeout=30)
            r.raise_for_status()
            html = response_text(r)
            pub = parse_publish_date(html)
            price = parse_price(html)
            grass = "草甘膦" in html
            print("NBS_ARTICLE", candidate, "grass", grass, "price", price, "bytes", len(r.content))
            if best is None:
                best = (pub, price, candidate, html)
            if price is not None:
                return pub, price, candidate, html
        except Exception as exc:
            failures.append((candidate, repr(exc)))
    if best is not None:
        return best
    raise RuntimeError(f"all article variants failed: {failures}")


def period_set_between(start: dt.date, end: dt.date):
    out = set()
    cur = dt.date(start.year, start.month, 1)
    while cur <= end:
        for part, day in (
            ("上旬", 10), ("中旬", 20), ("下旬", calendar.monthrange(cur.year, cur.month)[1]),
        ):
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

    requested_start = dt.date.fromisoformat(args.start)
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
            INDEX_ROOT, "official release-index archive", "NBS_OFFICIAL_WEB", "HIGH", "FETCHING",
            "PIT date=publication_date; archive layout before first discovered release is not assumed complete",
        ))
        con.execute("DELETE FROM industry_product_daily WHERE source_id=?", (SOURCE_ID,))
        con.execute("DELETE FROM industry_product_observation WHERE product_id=?", (PRODUCT_ID,))
        con.commit()

        releases = discover_releases(s, requested_start, end, args.max_index_pages)
        print("NBS_DISCOVERED", len(releases))
        ok, miss = [], []
        for i, rec in enumerate(releases, 1):
            period, period_end, url = rec["period"], rec["period_end"], rec["url"]
            try:
                pub, price, used_url, html = fetch_article(s, url)
                pub = pub or rec.get("listed_date")
            except Exception as exc:
                print("NBS_PAGE_ERR", period, url, repr(exc))
                miss.append({"period": period, "reason": "page_error", "url": url})
                continue

            if not pub or price is None:
                snippet = ""
                p = html.find("草甘膦") if html else -1
                if p >= 0:
                    snippet = re.sub(r"\s+", " ", html[max(0, p - 120):p + 300])[:500]
                print("NBS_PARSE_MISS", period, "pub", pub, "price", price, used_url, "snippet", snippet)
                con.execute(
                    "INSERT OR REPLACE INTO industry_product_observation VALUES(?,?,?,?,?,?,?,?,?)",
                    (PRODUCT_ID, period, period_end.isoformat(), pub.isoformat() if pub else None,
                     price, "元/吨", used_url, "PARSE_MISS", now),
                )
                con.commit()
                miss.append({"period": period, "reason": "parse_miss", "url": used_url})
                continue

            if pub < period_end - dt.timedelta(days=5) or pub > period_end + dt.timedelta(days=45):
                print("NBS_DATE_REJECT", period, period_end, pub, used_url)
                miss.append({"period": period, "reason": "bad_publish_date", "url": used_url})
                continue

            con.execute(
                "INSERT OR REPLACE INTO industry_product_observation VALUES(?,?,?,?,?,?,?,?,?)",
                (PRODUCT_ID, period, period_end.isoformat(), pub.isoformat(), price,
                 "元/吨", used_url, "OK", now),
            )
            con.execute("""INSERT OR REPLACE INTO industry_product_daily(
                date,product_id,product_name,category,price,unit,region,frequency,source_id,ingest_ts)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (pub.isoformat(), PRODUCT_ID, PRODUCT_NAME, "农药原药", price,
                 "元/吨", "全国", "旬度", SOURCE_ID, now),
            )
            con.commit()
            ok.append({"period": period, "period_end": period_end, "publication_date": pub, "price": price})
            print("NBS_OK", i, len(releases), period, pub, price)
            time.sleep(0.03)

        discovered_periods = {x["period"] for x in releases}
        got = {x["period"] for x in ok}
        discovered_coverage = len(got) / len(discovered_periods) if discovered_periods else 0.0
        requested_expected = period_set_between(requested_start, end)
        if releases:
            effective_start = min(x["period_end"] for x in releases)
            effective_expected = period_set_between(effective_start, end)
        else:
            effective_start = None
            effective_expected = set()

        mn, mx = con.execute(
            "SELECT MIN(date),MAX(date) FROM industry_product_daily WHERE source_id=?", (SOURCE_ID,)
        ).fetchone()
        audit = {
            "rows": len(ok),
            "requested_start": requested_start.isoformat(),
            "effective_archive_start": None if effective_start is None else effective_start.isoformat(),
            "requested_expected_periods": len(requested_expected),
            "official_index_discovered_periods": len(discovered_periods),
            "parsed_discovered_periods": len(got),
            "discovered_parse_coverage_ratio": discovered_coverage,
            "effective_expected_periods": len(effective_expected),
            "undiscovered_within_effective_window": len(effective_expected - discovered_periods),
            "min_publication_date": mn,
            "max_publication_date": mx,
            "parse_missing_samples": miss[:40],
            "archive_caveat": "pre-effective-start absence is not interpreted as proof that NBS did not publish the product",
        }
        Path(args.audit_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.audit_json).write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

        status = "FETCHED" if len(ok) >= 30 and discovered_coverage >= 0.70 else ("PARTIAL" if ok else "FAILED")
        con.execute(
            "UPDATE source_registry SET v1_status=?,notes=notes||? WHERE source_id=?",
            (status,
             f"; rows={len(ok)} discovered={len(discovered_periods)} parse_coverage={discovered_coverage:.3f} min={mn} max={mx}",
             SOURCE_ID),
        )
        con.commit()
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        if status != "FETCHED":
            raise SystemExit(2)
    finally:
        con.close()


if __name__ == "__main__":
    main()
