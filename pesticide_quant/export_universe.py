#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse,csv,sqlite3
from pathlib import Path

def exchange(code):
    if code.startswith(("60","68")):return "SH"
    if code.startswith(("92","43","83","87")):return "BJ"
    return "SZ"

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--db",required=True);ap.add_argument("--out",required=True);args=ap.parse_args()
    con=sqlite3.connect(args.db);names={r[0]:r[1] for r in con.execute("SELECT code,name FROM company_master")};current={r[0] for r in con.execute("SELECT code FROM company_master WHERE current_member=1")};hist={r[0] for r in con.execute("SELECT DISTINCT code FROM industry_membership_history")};codes=sorted(current|hist);con.close()
    rows=[{"code":c,"name":names.get(c,""),"exchange":exchange(c),"current_member_snapshot":int(c in current),"historical_member":int(c in hist),"universe_status":"OFFICIAL_SW_HISTORY_PLUS_CURRENT"} for c in codes]
    Path(args.out).parent.mkdir(parents=True,exist_ok=True)
    with open(args.out,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0].keys()));w.writeheader();w.writerows(rows)
    print({"universe_codes":len(rows),"historical_codes":len(hist),"current_codes":len(current)})
if __name__=="__main__":main()
