#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Direct-PDF full-text backfill for announcement event-chain V2.

Eastmoney's JSON content endpoint rate-limits after a small burst.  The public
PDF CDN is deterministic by art_code and was independently probed on both JSON
successes and JSON failures.  This loader therefore uses:

  https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf

It extracts text with pdftotext/poppler, preserves the audited title-event
`effective_date`, and then rebuilds the same V2 event-chain + daily feature
schema used by announcement_fulltext_v2.py.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import announcement_fulltext_v2 as v2

SOURCE_ID = "S021P"
VERSION = "ANN_CHAIN_FULLTEXT_PDF_V2"
PDF_TEMPLATE = "https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf"


def session():
    s = requests.Session()
    retry = Retry(
        total=4, connect=4, read=4, backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16))
    s.headers.update({
        "User-Agent": "Mozilla/5.0 pesticide-quant-pdf-v2",
        "Referer": "https://data.eastmoney.com/",
        "Accept": "application/pdf,*/*",
    })
    return s


def fetch_pdf_text(art_code: str, timeout: float = 35.0):
    url = PDF_TEMPLATE.format(art_code=art_code)
    s = session()
    try:
        r = s.get(url, timeout=timeout)
        r.raise_for_status()
        body = r.content
        ctype = str(r.headers.get("content-type") or "")
        if len(body) < 500 or not body.startswith(b"%PDF"):
            return {"art_code": art_code, "ok": False, "text": "", "url": url,
                    "bytes": len(body), "meta": f"not_pdf content_type={ctype}"}
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(body)
            tmp = f.name
        try:
            p = subprocess.run(
                ["pdftotext", "-layout", tmp, "-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=35,
            )
            text = p.stdout.decode("utf-8", errors="ignore").strip()
            # Event extraction only reads the first 25k chars; keeping a generous
            # ceiling controls SQLite size while retaining long contextual docs.
            text = text[:350000]
            ok = len(text) >= 80
            meta = json.dumps({
                "http_status": r.status_code,
                "content_type": ctype,
                "pdf_bytes": len(body),
                "pdftotext_rc": p.returncode,
                "stderr": p.stderr.decode("utf-8", errors="ignore")[:500],
            }, ensure_ascii=False)
            return {"art_code": art_code, "ok": ok, "text": text, "url": url,
                    "bytes": len(body), "meta": meta}
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception as exc:
        return {"art_code": art_code, "ok": False, "text": "", "url": url,
                "bytes": 0, "meta": repr(exc)}
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--max-rows", type=int, default=0)
    ap.add_argument("--audit-json")
    ap.add_argument("--manifest-csv")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    v2.ensure_schema(con)
    try:
        src = v2.candidate_rows(con, args.max_rows or None)
        if src.empty:
            raise SystemExit("No full-text candidates")

        todo = list(dict.fromkeys(src.art_code.astype(str)))
        print("PDF_FULLTEXT_CANDIDATES", len(src), "TODO", len(todo), "WORKERS", args.workers)
        fetched = []
        with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            fut = {ex.submit(fetch_pdf_text, art): art for art in todo}
            for i, f in enumerate(cf.as_completed(fut), 1):
                res = f.result()
                fetched.append(res)
                if i % 100 == 0 or i == len(todo):
                    ok = sum(x["ok"] for x in fetched)
                    print("PDF_FULLTEXT", i, len(todo), "ok", ok)

        now = dt.datetime.now().isoformat(timespec="seconds")
        src_idx = src.set_index("art_code")
        manifest = []
        for r in fetched:
            rr = src_idx.loc[r["art_code"]]
            rr = rr.iloc[0] if isinstance(rr, pd.DataFrame) else rr
            status = "PDF_OK" if r["ok"] else "PDF_FAILED"
            con.execute(
                """INSERT OR REPLACE INTO announcement_fulltext_v2(
                art_code,canonical_code,notice_date,effective_date,title,full_text,text_chars,
                attach_url,fetch_status,fetch_meta,fetched_at,classification_version)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["art_code"], rr.canonical_code, rr.notice_date, rr.effective_date, rr.title,
                 r["text"], len(r["text"]), r["url"], status, r["meta"], now, VERSION),
            )
            manifest.append({
                "canonical_code": rr.canonical_code,
                "art_code": r["art_code"], "notice_date": rr.notice_date,
                "effective_date": rr.effective_date, "title": rr.title,
                "fetch_status": status, "text_chars": len(r["text"]),
                "pdf_bytes": r["bytes"], "pdf_url": r["url"],
            })
        con.commit()

        full = pd.read_sql_query(
            "SELECT art_code,full_text,attach_url,fetch_status FROM announcement_fulltext_v2", con
        )
        full = full[full.art_code.isin(src.art_code)].copy()
        ev = v2.build_event_v2(src, full)
        con.execute("DELETE FROM announcement_event_v2")
        if len(ev):
            ev.to_sql("announcement_event_v2", con, if_exists="append", index=False)
        market = pd.read_sql_query(
            "SELECT trade_date,code,close_qfq FROM market_daily WHERE close_qfq IS NOT NULL ORDER BY code,trade_date", con
        )
        membership = pd.read_sql_query(
            "SELECT code,in_date,out_date FROM industry_membership_history", con
        )
        feat = v2.score_daily(ev, market, membership)
        con.execute("DELETE FROM announcement_feature_daily_v2")
        if len(feat):
            # preserve S021-compatible schema but identify PDF build in source registry/audit
            feat["source_id"] = SOURCE_ID
            feat.to_sql("announcement_feature_daily_v2", con, if_exists="append", index=False)
        con.execute(
            """INSERT OR REPLACE INTO source_registry(
            source_id,data_layer,source_name,url,coverage,source_type,reliability,v1_status,notes)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (SOURCE_ID, "announcement_fulltext", "Eastmoney direct announcement PDF",
             "https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf",
             f"candidate disclosures; {len(src)} art codes", "EASTMONEY_PDF_CDN", "MEDIUM",
             "FULLTEXT_PDF_V2_BUILT",
             "direct PDF fallback after JSON content API rate-limit; audited effective_date inherited"),
        )
        con.commit()

        ok = sum(r["ok"] for r in fetched)
        audit = {
            "source": SOURCE_ID,
            "version": VERSION,
            "candidate_rows": int(len(src)),
            "candidate_codes": int(src.canonical_code.nunique()),
            "pdf_ok": int(ok),
            "pdf_failed": int(len(fetched) - ok),
            "pdf_text_coverage": float(ok / max(len(fetched), 1)),
            "event_rows": int(len(ev)),
            "event_codes": int(ev.canonical_code.nunique()) if len(ev) else 0,
            "events_with_stage_delta": int((ev.stage_delta != 0).sum()) if len(ev) else 0,
            "events_with_progress": int(ev.progress_pct.notna().sum()) if len(ev) else 0,
            "long_setup_events": int((ev.long_setup_flag > 0).sum()) if len(ev) else 0,
            "positive_acceleration_events": int((ev.recent_acceleration_event > 0).sum()) if len(ev) else 0,
            "negative_acceleration_events": int((ev.negative_acceleration_event > 0).sum()) if len(ev) else 0,
            "feature_rows": int(len(feat)),
            "feature_codes": int(feat.code.nunique()) if len(feat) else 0,
            "pit_rule": "inherits audited title-event effective_date; next-market-day or later",
            "url_rule": PDF_TEMPLATE,
        }
        if args.audit_json:
            Path(args.audit_json).write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.manifest_csv:
            pd.DataFrame(manifest).sort_values(["notice_date", "canonical_code", "art_code"]).to_csv(
                args.manifest_csv, index=False, encoding="utf-8-sig"
            )
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        # Hard quality gate: do not accept a nominal V2 when full-text coverage is sparse.
        if audit["pdf_text_coverage"] < 0.70:
            raise SystemExit(4)
    finally:
        con.close()


if __name__ == "__main__":
    main()
