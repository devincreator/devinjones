#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse, json, sqlite3
from pathlib import Path
import numpy as np, pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import precision_score,recall_score,f1_score,roc_auc_score,accuracy_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

TECH=["ret_5d","ret_20d","ret_60d","vol_20d","ma20_gap","ma60_gap","turnover_z20","amount_z20","range_20d"]
FIN=["revenue_yoy_pct","net_profit_yoy_pct","roe_weighted_pct","gross_margin_pct"]
LABEL_H=60

def load(con):
    m=pd.read_sql_query("""SELECT trade_date,code,open_qfq,high_qfq,low_qfq,close_qfq,
      volume_shares,amount_cny,turnover_pct FROM market_daily
      WHERE close_qfq IS NOT NULL AND (is_trade IS NULL OR is_trade=1)
      ORDER BY code,trade_date""",con)
    mem=pd.read_sql_query("""SELECT code,in_date,out_date FROM industry_membership_history ORDER BY code,in_date""",con)
    fin=pd.read_sql_query("""SELECT code,report_period,ann_date,available_date,revision_id,
      revenue_yoy_pct,net_profit_yoy_pct,roe_weighted_pct,gross_margin_pct
      FROM financial_quarterly ORDER BY code,available_date,report_period,revision_id""",con)
    m["trade_date"]=pd.to_datetime(m["trade_date"])
    for c in ["in_date","out_date"]: mem[c]=pd.to_datetime(mem[c],errors="coerce")
    if not fin.empty:
        fin["available_date"]=pd.to_datetime(fin["available_date"]); fin["ann_date"]=pd.to_datetime(fin["ann_date"])
    return m,mem,fin

def gate(con,m,mem):
    job=con.execute("SELECT status FROM ingestion_job WHERE job_id='J003'").fetchone(); j003=job[0] if job else None
    mcodes=set(mem["code"].astype(str)); market_codes=set(m["code"].astype(str)); missing=sorted(mcodes-market_codes)
    return {"j003_status":j003,"membership_rows":len(mem),"membership_codes":len(mcodes),
      "market_rows":len(m),"market_codes":len(market_codes),"missing_membership_market_codes":missing,
      "formal_ok":bool(j003=="LOADED" and len(mem)>0 and len(m)>5000 and not missing)}

def tech_features(m):
    out=[]
    for code,g in m.groupby("code",sort=False):
        g=g.sort_values("trade_date").copy(); c=g["close_qfq"];h=g["high_qfq"];l=g["low_qfq"];t=g["turnover_pct"];a=g["amount_cny"]
        g["ret_5d"]=c.pct_change(5,fill_method=None);g["ret_20d"]=c.pct_change(20,fill_method=None);g["ret_60d"]=c.pct_change(60,fill_method=None)
        g["vol_20d"]=c.pct_change(fill_method=None).rolling(20).std(ddof=0);g["ma20_gap"]=c/c.rolling(20).mean()-1;g["ma60_gap"]=c/c.rolling(60).mean()-1
        g["turnover_z20"]=(t-t.rolling(20).mean())/t.rolling(20).std(ddof=0).replace(0,np.nan)
        g["amount_z20"]=(a-a.rolling(20).mean())/a.rolling(20).std(ddof=0).replace(0,np.nan)
        g["range_20d"]=h.rolling(20).max()/l.rolling(20).min()-1;out.append(g)
    return pd.concat(out,ignore_index=True) if out else pd.DataFrame()

def feature_dates(tf,mem):
    chunks=[];tg={k:g for k,g in tf.groupby("code",sort=False)}
    for code,gmem in mem.groupby("code",sort=False):
        g=tg.get(code)
        if g is None:continue
        for r in gmem.itertuples(index=False):
            mask=g["trade_date"].ge(r.in_date)
            if pd.notna(r.out_date):mask &= g["trade_date"].le(r.out_date)
            z=g.loc[mask].copy()
            if len(z):chunks.append(z)
    if not chunks:return pd.DataFrame()
    return pd.concat(chunks,ignore_index=True).sort_values(["code","trade_date"]).drop_duplicates(["code","trade_date"])

def join_fin(feat,fin):
    if feat.empty:return feat
    if fin.empty:
        for c in FIN:feat[c]=np.nan
        feat["financial_available_date"]=pd.NaT;return feat
    out=[];fg={k:g.sort_values("available_date") for k,g in fin.groupby("code")}
    for code,g in feat.groupby("code",sort=False):
        f=fg.get(code);g=g.sort_values("trade_date").copy()
        if f is None or f.empty:
            for c in FIN:g[c]=np.nan
            g["financial_available_date"]=pd.NaT;out.append(g);continue
        f=f.sort_values(["available_date","report_period","revision_id"]).drop_duplicates("available_date",keep="last")
        f=f[["available_date"]+FIN].rename(columns={"available_date":"financial_available_date"})
        out.append(pd.merge_asof(g,f,left_on="trade_date",right_on="financial_available_date",direction="backward",allow_exact_matches=True))
    x=pd.concat(out,ignore_index=True);leak=x[x["financial_available_date"].notna()&(x["financial_available_date"]>x["trade_date"])]
    if len(leak):raise RuntimeError(f"finance lookahead {len(leak)}")
    return x

def labels(feat,full_market,op_ret=.15,op_dd=-.10,risk_dd=-.15):
    rows=[];mg={k:g.sort_values("trade_date").reset_index(drop=True) for k,g in full_market.groupby("code",sort=False)}
    for code,g in feat.groupby("code",sort=False):
        market=mg.get(code)
        if market is None:continue
        pos={d:i for i,d in enumerate(market["trade_date"])};close=market["close_qfq"].to_numpy(float);low=market["low_qfq"].to_numpy(float);high=market["high_qfq"].to_numpy(float);dates=market["trade_date"].to_numpy()
        g=g.copy();fr=[];dd=[];up=[];ends=[]
        for d in g["trade_date"]:
            i=pos.get(d);j=None if i is None else i+LABEL_H
            if i is None or j>=len(market):fr.append(np.nan);dd.append(np.nan);up.append(np.nan);ends.append(pd.NaT);continue
            base=close[i];fr.append(close[j]/base-1);dd.append(np.nanmin(low[i+1:j+1])/base-1);up.append(np.nanmax(high[i+1:j+1])/base-1);ends.append(pd.Timestamp(dates[j]))
        g["fwd_ret_60d"]=fr;g["max_drawdown_60d"]=dd;g["max_upside_60d"]=up;g["label_end_date"]=ends;valid=g["fwd_ret_60d"].notna()
        g["opportunity_label"]=np.where(valid,((g["fwd_ret_60d"]>=op_ret)&(g["max_drawdown_60d"]>op_dd)).astype(int),np.nan)
        g["risk_label"]=np.where(valid,(g["max_drawdown_60d"]<=risk_dd).astype(int),np.nan);rows.append(g)
    return pd.concat(rows,ignore_index=True) if rows else pd.DataFrame()

def model_prob(train,test,cols,target):
    pipe=Pipeline([("imp",SimpleImputer(strategy="median",keep_empty_features=True)),("sc",StandardScaler()),("lr",LogisticRegression(max_iter=2000,class_weight="balanced"))])
    pipe.fit(train[cols],train[target].astype(int));return pipe.predict_proba(test[cols])[:,1]

def metr(y,p):
    pred=(p>=.5).astype(int);y=np.asarray(y).astype(int)
    return {"n":len(y),"positive_rate":float(y.mean()),"predicted_positive":int(pred.sum()),"accuracy":float(accuracy_score(y,pred)),"precision_win_rate":float(precision_score(y,pred,zero_division=0)),"recall":float(recall_score(y,pred,zero_division=0)),"f1":float(f1_score(y,pred,zero_division=0)),"auc":float(roc_auc_score(y,p)) if len(np.unique(y))>1 else None}

def walk(panel):
    panel=panel.copy();panel["year"]=panel["trade_date"].dt.year;years=sorted(panel["year"].dropna().unique());metrics=[];preds=[];sets={"TECH":TECH,"FIN":FIN,"TECH_FIN":TECH+FIN}
    for target in ["opportunity_label","risk_label"]:
        v=panel[panel[target].notna()&panel["label_end_date"].notna()].copy()
        for y in years[3:]:
            test=v[v["year"]==y].copy()
            if test.empty:continue
            start=test["trade_date"].min();train=v[(v["trade_date"]<start)&(v["label_end_date"]<start)].copy()
            if train.empty or train[target].nunique()<2:continue
            for name,cols in sets.items():
                if train[cols].notna().sum().sum()==0:continue
                p=model_prob(train,test,cols,target);m=metr(test[target],p);m.update({"target":target,"model":name,"test_year":int(y),"train_n":len(train),"train_last_label_end":train["label_end_date"].max().date().isoformat(),"test_start":start.date().isoformat()});metrics.append(m)
                z=test[["trade_date","code","fwd_ret_60d","max_drawdown_60d","opportunity_label","risk_label"]].copy();z["target"]=target;z["model"]=name;z["test_year"]=y;z["prob"]=p;z["pred"]=(p>=.5).astype(int);preds.append(z)
    return pd.DataFrame(metrics),pd.concat(preds,ignore_index=True) if preds else pd.DataFrame()

def aggregate(preds):
    rows=[]
    if preds.empty:return pd.DataFrame()
    for (t,m),g in preds.groupby(["target","model"]):
        z=metr(g[t].astype(int),g["prob"]);z.update({"target":t,"model":m,"years":int(g["test_year"].nunique())});rows.append(z)
    return pd.DataFrame(rows)

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--db",required=True);ap.add_argument("--outdir",required=True);ap.add_argument("--allow-partial",action="store_true");args=ap.parse_args()
    out=Path(args.outdir);out.mkdir(parents=True,exist_ok=True);con=sqlite3.connect(args.db);m,mem,fin=load(con);g=gate(con,m,mem);(out/"gate.json").write_text(json.dumps(g,ensure_ascii=False,indent=2),encoding="utf-8");print(json.dumps(g,ensure_ascii=False,indent=2))
    if not g["formal_ok"] and not args.allow_partial:raise SystemExit(2)
    if mem.empty or m.empty:raise SystemExit(3)
    tf=tech_features(m);feat=feature_dates(tf,mem);feat=join_fin(feat,fin);panel=labels(feat,m);metrics,preds=walk(panel);agg=aggregate(preds)
    panel.to_csv(out/"pit_panel.csv",index=False,encoding="utf-8-sig");metrics.to_csv(out/"walk_forward_metrics_by_year.csv",index=False,encoding="utf-8-sig");agg.to_csv(out/"walk_forward_metrics_overall.csv",index=False,encoding="utf-8-sig");preds.to_csv(out/"walk_forward_predictions.csv",index=False,encoding="utf-8-sig")
    summary={"status":"FORMAL" if g["formal_ok"] else "PARTIAL","gate":g,"panel_rows":len(panel),"panel_codes":int(panel["code"].nunique()),"aggregate":agg.to_dict("records")};(out/"backtest_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8");print(json.dumps(summary,ensure_ascii=False,indent=2));con.close()

if __name__=="__main__":main()
