#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the official NBS PIT loader with browser-compatible article delivery.

The NBS release index is stable with ordinary requests, but GitHub-hosted
runners receive an alternate ~30-40 KB article shell for many release pages.
A three-date validation probe showed curl_cffi/Chrome delivery returns the full
official article and exact known glyphosate prices for 2022, 2025, and 2026.

This wrapper changes only the HTTP transport for release articles. Discovery,
publication-date PIT rules, price parsing, source URLs, database schema, and
coverage audits remain in :mod:`industry_cycle_nbs`.
"""
from __future__ import annotations

from curl_cffi import requests as curl_requests

import industry_cycle_nbs as base

_BROWSER = curl_requests.Session(impersonate="chrome")
_HEADERS = {
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    "Referer": "https://www.stats.gov.cn/",
}


def decode(content: bytes) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            pass
    return content.decode("utf-8", errors="replace")


def fetch_article_browser(_ordinary_session, url):
    failures = []
    best = None
    for candidate in base.article_variants(url):
        try:
            r = _BROWSER.get(candidate, timeout=40, headers=_HEADERS)
            r.raise_for_status()
            html = decode(r.content)
            pub = base.parse_publish_date(html)
            price = base.parse_price(html)
            has_product = "草甘膦" in html
            print(
                "NBS_BROWSER_ARTICLE", candidate,
                "status", r.status_code,
                "bytes", len(r.content),
                "glyphosate", has_product,
                "price", price,
            )
            if best is None:
                best = (pub, price, candidate, html)
            if price is not None:
                return pub, price, candidate, html
        except Exception as exc:
            failures.append((candidate, repr(exc)))
    if best is not None:
        return best
    raise RuntimeError(f"all browser-compatible article variants failed: {failures}")


def main():
    base.fetch_article = fetch_article_browser
    base.main()


if __name__ == "__main__":
    main()
