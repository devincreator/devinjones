#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probe official NBS article delivery with curl_cffi browser impersonation."""
from __future__ import annotations

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests

TESTS = [
    ("2022-08-下旬", "https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1901569.html", 60142.9),
    ("2025-06-下旬", "https://www.stats.gov.cn/sj/zxfb/202507/t20250703_1960315.html", 25208.3),
    ("2026-08-上旬", "https://www.stats.gov.cn/sj/zxfbhjd/202608/t20260813_1965025.html", 26000.0),
]


def decode(content: bytes) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            pass
    return content.decode("utf-8", errors="replace")


def nums(text: str):
    out = []
    for raw in re.findall(r"(?<!\d)-?\d[\d,]*(?:\.\d+)?", text or ""):
        try:
            x = float(raw.replace(",", ""))
        except ValueError:
            continue
        if 1000 < x < 500000:
            out.append(x)
    return out


def parse_price(html: str):
    soup = BeautifulSoup(html, "lxml")
    for node in soup.find_all(string=re.compile("草甘膦")):
        tr = node.find_parent("tr")
        if tr is not None:
            xs = nums(" ".join(tr.stripped_strings))
            if xs:
                return xs[0]
        parent = node.parent
        for _ in range(6):
            if parent is None:
                break
            txt = " ".join(parent.stripped_strings)
            if "草甘膦" in txt:
                xs = nums(txt)
                if xs:
                    return xs[0]
            parent = parent.parent
    text = " ".join(soup.stripped_strings)
    p = text.find("草甘膦")
    return (nums(text[p:p+600]) or [None])[0] if p >= 0 else None


def main():
    rows = []
    for label, url, expected in TESTS:
        try:
            r = requests.get(
                url,
                impersonate="chrome",
                timeout=40,
                headers={
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
                    "Referer": "https://www.stats.gov.cn/",
                },
            )
            html = decode(r.content)
            price = parse_price(html)
            row = {
                "label": label,
                "url": url,
                "status": r.status_code,
                "bytes": len(r.content),
                "contains_glyphosate": "草甘膦" in html,
                "parsed_price": price,
                "expected_price": expected,
                "matches_expected": price is not None and abs(price - expected) < 0.11,
                "server": r.headers.get("server"),
                "content_type": r.headers.get("content-type"),
            }
        except Exception as exc:
            row = {"label": label, "url": url, "error": repr(exc), "matches_expected": False}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))

    passed = sum(bool(r.get("matches_expected")) for r in rows)
    summary = {"passed": passed, "total": len(rows), "all_passed": passed == len(rows), "rows": rows}
    Path("work").mkdir(exist_ok=True)
    Path("work/nbs_curl_probe.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if passed != len(rows):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
