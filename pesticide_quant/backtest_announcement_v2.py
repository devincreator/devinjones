#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PIT incremental walk-forward for title V1.1 vs full-text announcement V2."""
from __future__ import annotations
import argparse,json,sqlite3
from pathlib import Path
import numpy as np
import pandas as pd
import backtest as base
import backtest_full as full
import backtest_announcement as ann1

FT=["ft_long_term_setup_score","ft_recent_acceleration_30d","ft_recent_acceleration_90d","ft_milestone_delta_90d","ft_progress_delta_90d","ft_commercialization_90d","ft_negative_acceleration_90d","ft_topic_depth_365d","ft_chain_confidence","ft_text_novelty_90d","ft_price_digestion_score","ft_event_excess_ret_since_key","ft_information_price_gap","ft_days_since_inflection","ft_event_count_90d","ft_available"]
FT_RISK=["ft_negative_acceleration_90d","ft_chain_confidence","ft_text_novelty_90d","ft_price_digestion_score","ft_event_excess_ret_since_key","ft_information_price_gap","ft_days_since_inflection","ft_event_count_90d","ft_available"]

def load_ft(con):
 try:x=pd.read_sql_query("SELECT * FROM announcement_feature_daily_v2 ORDER BY code,trade_date",con)
 except Exception:return pd.DataFrame()
 if len(x):x.trade_date=pd.to_datetime(x.trade_date)
 return x

def join_ft(panel,ft):
 p=panel.copy()
 if ft.empty:
  for c in FT:p[c]=0. if c=="ft_available" else np.nan
  return p
 p=p.merge(ft[["trade_date","code"]+FT],on=["trade_date","code"],how="left"); p.ft_available=p.ft_available.fillna(0.); return p

def model_sets(target):
 tf=base.TECH+base.FIN
 if target=="opportunity_label":return {"TECH_FIN":tf,"TECH_FIN_TITLE_V11":tf+ann1.ANN,"ANN_FULLTEXT_V2":FT,"TECH_FIN_FULLTEXT_V2":tf+FT,"TECH_FIN_TITLE_FULLTEXT_V2":tf+ann1.ANN+FT}
 return {"TECH_FIN":tf,"TECH_FIN_TITLE_V11":tf+ann1.ANN,"RISK_FULLTEXT_V2":FT_RISK,"TECH_FIN_RISK_FULLTEXT_V2":tf+FT_RISK,"TECH_FIN_TITLE_RISK_FULLTEXT_V2":tf+ann1.ANN+FT_RISK}

def walk(panel):
 panel=panel.copy(); panel["year"]=panel.trade_date.dt.year; years=sorted(panel.year.dropna().unique()); metrics=[]; preds=[]
 for target in ["opportunity_label","risk_label"]:
  valid=panel[panel[target].notna()&panel.label_end_date.notna()].copy(); sets=model_sets(target)
  for y in years[3:]:
   test=valid[valid.year==y].copy()
   if test.empty:continue
   start=test.trade_date.min(); train=valid[(valid.trade_date<start)&(valid.label_end_date<start)].copy()
   if train.empty or train[target].nunique()<2:continue
   for name,cols in sets.items():
    if train[cols].notna().sum().sum()==0:continue
    prob=base.model_prob(train,test,cols,target); met=base.metr(test[target],prob); met.update({"target":target,"model":name,"test_year":int(y),"train_n":int(len(train)),"test_n":int(len(test)),"train_last_label_end":train.label_end_date.max().date().isoformat(),"test_start":start.date().isoformat()}); metrics.append(met)
    z=test[["trade_date","code","label_end_date","fwd_ret_60d","max_drawdown_60d","max_upside_60d","opportunity_label","risk_label"]].copy(); z["target"]=target; z["model"]=name; z["test_year"]=int(y); z["prob"]=prob; z["pred"]=(prob>=.5).astype(int); preds.append(z)
 return pd.DataFrame(metrics),pd.concat(preds,ignore_index=True) if preds else pd.DataFrame()

def aggregate(preds):
 rows=[]
 for (target,model),g in preds.groupby(["target","model"]):
  m=base.metr(g[target].astype(int),g.prob); m.update({"target":target,"model":model,"years":int(g.test_year.nunique())}); rows.append(m)
 return pd.DataFrame(rows)

def pair_delta(agg,target,left,right,label):
 a=agg[(agg.target==target)&(agg.model==left)]; b=agg[(agg.target==target)&(agg.model==right)]
 if a.empty or b.empty:return None
 a=a.iloc[0]; b=b.iloc[0]; return {"target":target,"comparison":label,"base_model":left,"new_model":right,"precision_base":a.precision_win_rate,"precision_new":b.precision_win_rate,"precision_delta":b.precision_win_rate-a.precision_win_rate,"auc_base":a.auc,"auc_new":b.auc,"auc_delta":None if pd.isna(a.auc) or pd.isna(b.auc) else b.auc-a.auc,"recall_delta":b.recall-a.recall,"f1_delta":b.f1-a.f1}

def deltas(agg):
 rows=[]; pairs=[("opportunity_label","TECH_FIN","TECH_FIN_TITLE_V11","title_v11_vs_tf"),("opportunity_label","TECH_FIN","TECH_FIN_FULLTEXT_V2","fulltext_v2_vs_tf"),("opportunity_label","TECH_FIN_TITLE_V11","TECH_FIN_TITLE_FULLTEXT_V2","v2_increment_over_title"),("risk_label","TECH_FIN","TECH_FIN_TITLE_V11","title_v11_vs_tf"),("risk_label","TECH_FIN","TECH_FIN_RISK_FULLTEXT_V2","risk_v2_vs_tf"),("risk_label","TECH_FIN_TITLE_V11","TECH_FIN_TITLE_RISK_FULLTEXT_V2","risk_v2_increment_over_title")]
 for p in pairs:
  r=pair_delta(agg,*p)
  if r:rows.append(r)
 return pd.DataFrame(rows)

def main():
 ap=argparse.ArgumentParser(); ap.add_argument("--db",required=True); ap.add_argument("--outdir",required=True); args=ap.parse_args(); out=Path(args.outdir); out.mkdir(parents=True,exist_ok=True); con=sqlite3.connect(args.db)
 try:
  m,mem,fin=base.load(con); ann=ann1.load_ann(con); ft=load_ft(con); gate={"market_rows":int(len(m)),"membership_rows":int(len(mem)),"finance_rows":int(len(fin)),"title_rows":int(len(ann)),"title_codes":int(ann.code.nunique()) if len(ann) else 0,"fulltext_rows":int(len(ft)),"fulltext_codes":int(ft.code.nunique()) if len(ft) else 0,"ok":bool(len(m)>0 and len(mem)>0 and len(fin)>0 and len(ann)>0 and len(ft)>0)}; (out/"gate_v2.json").write_text(json.dumps(gate,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps(gate,ensure_ascii=False,indent=2))
  if not gate["ok"]:raise SystemExit(2)
  tf=base.tech_features(m); feat=base.feature_dates(tf,mem); feat=base.join_fin(feat,fin); feat=ann1.join_ann(feat,ann); feat=join_ft(feat,ft); panel=base.labels(feat,m); metrics,preds=walk(panel); agg=aggregate(preds); events=full.nonoverlap_metrics(preds); delta=deltas(agg)
  metrics.to_csv(out/"metrics_by_year_v2.csv",index=False,encoding="utf-8-sig"); agg.to_csv(out/"metrics_overall_v2.csv",index=False,encoding="utf-8-sig"); events.to_csv(out/"nonoverlap_event_metrics_v2.csv",index=False,encoding="utf-8-sig"); delta.to_csv(out/"incremental_delta_v2.csv",index=False,encoding="utf-8-sig"); preds.to_csv(out/"predictions_v2.csv",index=False,encoding="utf-8-sig")
  summary={"status":"ANNOUNCEMENT_FULLTEXT_V2_INCREMENTAL_TEST","gate":gate,"panel_rows":int(len(panel)),"panel_codes":int(panel.code.nunique()),"aggregate":agg.to_dict("records"),"incremental_delta":delta.to_dict("records"),"interpretation_rule":"opportunity: keep V2 only if it improves OOS over both TECH_FIN and title V1.1 with stable non-overlap events; risk: judge the negative-only V2 set separately."}; (out/"summary_v2.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps(summary,ensure_ascii=False,indent=2))
 finally:con.close()
if __name__=="__main__":main()
