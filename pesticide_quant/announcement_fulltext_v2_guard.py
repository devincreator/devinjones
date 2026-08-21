#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic semantic guard for full-text V2 before backtest.

Prevents substring/backstory reversals such as 复产->停产, 解除质押->质押,
回复问询函->问询. Rebuilds V2 events/features from already fetched full text.
"""
import argparse,json,sqlite3
from pathlib import Path
import pandas as pd
import announcement_fulltext_v2 as v2

def guarded_classify(title,text,title_row):
 title_n=v2.strip_text(title); lead=v2.strip_text(text[:6000]); probe=title_n+'\n'+lead
 if any(k in title_n for k in ['复产','恢复生产','解除停产','整改完成','恢复正常生产']):return 'RECOVERY',7,1,3.5
 if '解除质押' in title_n or '解除股份质押' in title_n:return 'PLEDGE_RECOVERY',5,1,2.0
 if any(k in title_n for k in ['回复问询函','问询函回复','回复关注函','回函']):return 'INQUIRY_REPLY',4,1,2.0
 for stage,hard,cat,kws in v2.NEG_RULES:
  if any(k in title_n for k in kws) or any(k in lead[:3000] for k in kws):return cat,stage,-1,hard
 best=None
 for stage,hard,cat,kws in v2.POS_RULES:
  if any(k in probe for k in kws):
   cand=(stage,hard,cat)
   if best is None or cand[0]>best[0] or (cand[0]==best[0] and cand[1]>best[1]):best=cand
 if best:return best[2],best[0],1,best[1]
 return str(title_row.category),int(title_row.stage),int(title_row.direction),float(title_row.hardness)

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--db',required=True); ap.add_argument('--audit-json'); args=ap.parse_args(); con=sqlite3.connect(args.db)
 try:
  v2.ensure_schema(con); src=v2.candidate_rows(con,None); full=pd.read_sql_query("SELECT art_code,full_text,attach_url,fetch_status FROM announcement_fulltext_v2",con); full=full[full.art_code.isin(src.art_code)].copy(); old=pd.read_sql_query("SELECT art_code,category_v2,stage_v2,direction_v2 FROM announcement_event_v2",con)
  v2.classify_full=guarded_classify; ev=v2.build_event_v2(src,full); con.execute('DELETE FROM announcement_event_v2'); ev.to_sql('announcement_event_v2',con,if_exists='append',index=False) if len(ev) else None; market=pd.read_sql_query("SELECT trade_date,code,close_qfq FROM market_daily WHERE close_qfq IS NOT NULL ORDER BY code,trade_date",con); membership=pd.read_sql_query("SELECT code,in_date,out_date FROM industry_membership_history",con); feat=v2.score_daily(ev,market,membership); con.execute('DELETE FROM announcement_feature_daily_v2 WHERE source_id=?',(v2.SOURCE_ID,)); feat.to_sql('announcement_feature_daily_v2',con,if_exists='append',index=False) if len(feat) else None; con.commit(); changed=0; reversal_fixed=0
  if len(old) and len(ev):
   z=old.merge(ev[['art_code','category_v2','stage_v2','direction_v2']],on='art_code',suffixes=('_old','_new')); changed=int(((z.category_v2_old!=z.category_v2_new)|(z.stage_v2_old!=z.stage_v2_new)|(z.direction_v2_old!=z.direction_v2_new)).sum()); reversal_fixed=int(((z.direction_v2_old<0)&(z.direction_v2_new>0)).sum())
  audit={'version':'ANN_CHAIN_FULLTEXT_V2_GUARDED','candidate_rows':int(len(src)),'fulltext_ok':int((full.fetch_status=='OK').sum()),'events':int(len(ev)),'changed_from_unguarded':changed,'negative_to_positive_repairs':reversal_fixed,'feature_rows':int(len(feat)),'feature_codes':int(feat.code.nunique()) if len(feat) else 0,'guards':['recovery_before_shutdown','unpledge_before_pledge','inquiry_reply_before_inquiry','negative_fulltext_only_in_first_3000_chars']}
  if args.audit_json:Path(args.audit_json).write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
  print(json.dumps(audit,ensure_ascii=False,indent=2))
 finally:con.close()
if __name__=='__main__':main()
