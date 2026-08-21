#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, datetime as dt, difflib, html, json, os, re, sqlite3, subprocess, tempfile, time
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

QUERY_URL='https://www.cninfo.com.cn/new/hisAnnouncement/query'
ORG_URL='https://www.cninfo.com.cn/new/data/szse_stock.json'
PDF_BASE='https://static.cninfo.com.cn/'
SAMPLE={'600486':'扬农化工','002258':'利尔化学','002250':'联化科技','000525':'红太阳'}
KEYWORDS=('项目','投产','试生产','达产','扩产','产能','合同','订单','中标','业绩','修正','停产','复产','延期','终止','处罚','立案','诉讼','减持','回购','质押','问询','回复')

def session():
    s=requests.Session(); retry=Retry(total=5,connect=5,read=5,backoff_factor=.8,status_forcelist=(429,500,502,503,504),allowed_methods=frozenset(['GET','POST']))
    s.mount('https://',HTTPAdapter(max_retries=retry,pool_connections=8,pool_maxsize=8))
    s.headers.update({'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/127 Safari/537.36','Accept':'application/json,text/javascript,*/*;q=0.01','X-Requested-With':'XMLHttpRequest','Referer':'https://www.cninfo.com.cn/new/disclosure/stock','Origin':'https://www.cninfo.com.cn'})
    return s

def clean_title(x):
    x=html.unescape(re.sub(r'<[^>]+>','',str(x or '')))
    x=re.sub(r'\s+','',x)
    x=re.sub(r'[：:，,。；;（）()【】\[\]《》“”"\'·—-]','',x)
    return x

def ann_date(ms):
    if ms is None:return None
    try:return dt.datetime.fromtimestamp(float(ms)/1000,tz=dt.timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    except Exception:return str(ms)[:10]

def get_org_map(s):
    r=s.get(ORG_URL,timeout=25); r.raise_for_status(); d=r.json(); return {str(x.get('code')):str(x.get('orgId')) for x in d.get('stockList',[]) if x.get('code') and x.get('orgId')}

def query_chunk(s,code,org_id,start,end):
    column='sse' if code.startswith('6') else 'szse'; plate='sh' if code.startswith('6') else 'sz'
    out=[]; audit=[]; page=1; total=None
    while True:
        data={'pageNum':page,'pageSize':30,'tabName':'fulltext','column':column,'stock':f'{code},{org_id}','searchkey':'','secid':'','plate':plate,'category':'','trade':'','seDate':f'{start}~{end}','sortName':'','sortType':'','isHLtitle':'true'}
        r=s.post(QUERY_URL,data=data,timeout=35); status=r.status_code; r.raise_for_status(); payload=r.json(); anns=payload.get('announcements') or []
        if total is None: total=int(payload.get('totalAnnouncement') or 0)
        audit.append({'code':code,'start':start,'end':end,'page':page,'http_status':status,'returned':len(anns),'reported_total':total})
        for a in anns:
            out.append({'code':code,'name':SAMPLE.get(code,''),'org_id':org_id,'announcement_id':a.get('announcementId'),'date':ann_date(a.get('announcementTime')),'title':re.sub(r'<[^>]+>','',str(a.get('announcementTitle') or '')),'adjunct_url':a.get('adjunctUrl'),'adjunct_size':a.get('adjunctSize'),'adjunct_type':a.get('adjunctType'),'sec_code':a.get('secCode'),'sec_name':a.get('secName')})
        if not anns or len(out)>=total or len(anns)<30: break
        page+=1; time.sleep(.15)
        if page>100: break
    return out,audit,total

def fetch_all(s,start_year,end_date):
    orgs=get_org_map(s); rows=[]; audits=[]
    for code in SAMPLE:
        org=orgs.get(code)
        if not org: raise RuntimeError(f'no orgId for {code}')
        for year in range(start_year,int(end_date[:4])+1):
            a=f'{year}-01-01'; b=f'{year}-12-31' if year<int(end_date[:4]) else end_date
            got,au,total=query_chunk(s,code,org,a,b); rows.extend(got); audits.extend(au)
            actual=len(got); audits.append({'code':code,'start':a,'end':b,'page':'SUMMARY','http_status':200,'returned':actual,'reported_total':total,'complete':actual==total})
            print('CNINFO',code,year,actual,'/',total,flush=True); time.sleep(.25)
    df=pd.DataFrame(rows).drop_duplicates(['code','announcement_id']) if rows else pd.DataFrame()
    return df,pd.DataFrame(audits)

def load_eastmoney(db,start,end):
    con=sqlite3.connect(db)
    try:
        q='SELECT canonical_code AS code, notice_date AS date, title, art_code FROM announcement_raw WHERE canonical_code IN (%s) AND notice_date>=? AND notice_date<=?' % ','.join('?'*len(SAMPLE))
        params=list(SAMPLE)+[start,end]; x=pd.read_sql_query(q,con,params=params)
    finally:con.close()
    x['date']=pd.to_datetime(x['date']).dt.date.astype(str)
    return x.drop_duplicates(['code','art_code'])

def match_rows(cn,em):
    matches=[]; used=set()
    em=em.copy(); em['d']=pd.to_datetime(em.date); em['norm']=em.title.map(clean_title)
    for i,r in cn.iterrows():
        d=pd.Timestamp(r.date); cand=em[(em.code==r.code)&((em.d-d).abs()<=pd.Timedelta(days=2))]
        best=None
        for j,e in cand.iterrows():
            if j in used: continue
            score=difflib.SequenceMatcher(None,clean_title(r.title),e['norm']).ratio()
            if best is None or score>best[0]:best=(score,j,e)
        if best and best[0]>=.72:
            score,j,e=best; used.add(j); matches.append({'code':r.code,'cninfo_id':r.announcement_id,'cninfo_date':r.date,'cninfo_title':r.title,'eastmoney_art_code':e.art_code,'eastmoney_date':e.date,'eastmoney_title':e.title,'title_similarity':score})
    return pd.DataFrame(matches),used

def pdf_sample(cn,per_code=6):
    picks=[]
    for code,g in cn.sort_values('date').groupby('code'):
        cand=[]
        if len(g): cand += [g.iloc[0],g.iloc[-1]]
        kg=g[g.title.fillna('').apply(lambda t:any(k in t for k in KEYWORDS))]
        if len(kg):
            idx=list(dict.fromkeys([0,len(kg)//3,(2*len(kg))//3,len(kg)-1]))
            cand += [kg.iloc[i] for i in idx]
        seen=set()
        for r in cand:
            if r.announcement_id in seen:continue
            seen.add(r.announcement_id); picks.append(r)
            if len(seen)>=per_code:break
    return picks

def test_pdfs(s,cn):
    out=[]
    for r in pdf_sample(cn):
        rel=str(r.adjunct_url or '').lstrip('/'); url=PDF_BASE+rel
        ok=False; chars=0; status=None; size=0; err=''
        try:
            resp=s.get(url,timeout=40); status=resp.status_code; size=len(resp.content); resp.raise_for_status()
            with tempfile.TemporaryDirectory() as td:
                pdf=Path(td)/'a.pdf'; txt=Path(td)/'a.txt'; pdf.write_bytes(resp.content)
                cp=subprocess.run(['pdftotext','-layout','-enc','UTF-8',str(pdf),str(txt)],capture_output=True,text=True,timeout=45)
                text=txt.read_text(encoding='utf-8',errors='ignore') if txt.exists() else ''
                chars=len(re.sub(r'\s+','',text)); ok=resp.content[:4]==b'%PDF' and chars>=100
                if cp.returncode!=0:err=cp.stderr[-300:]
        except Exception as e:err=repr(e)
        out.append({'code':r.code,'date':r.date,'title':r.title,'announcement_id':r.announcement_id,'url':url,'http_status':status,'bytes':size,'text_chars':chars,'pdf_text_ok':ok,'error':err})
        print('PDF',r.code,r.date,ok,chars,flush=True); time.sleep(.1)
    return pd.DataFrame(out)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--db',required=True); ap.add_argument('--outdir',required=True); ap.add_argument('--start',default='2020-01-01'); ap.add_argument('--end',default='2026-08-21'); args=ap.parse_args()
    out=Path(args.outdir); out.mkdir(parents=True,exist_ok=True); s=session()
    cn,audit=fetch_all(s,int(args.start[:4]),args.end); em=load_eastmoney(args.db,args.start,args.end); matches,used=match_rows(cn,em); pdf=test_pdfs(s,cn)
    m_ids=set(matches.cninfo_id) if len(matches) else set(); cn_only=cn[~cn.announcement_id.isin(m_ids)].copy(); em_only=em[~em.index.isin(used)].copy()
    summaries=[]
    for code,name in SAMPLE.items():
        a=cn[cn.code==code]; b=em[em.code==code]; mm=matches[matches.code==code] if len(matches) else matches
        summaries.append({'code':code,'name':name,'cninfo_rows':len(a),'eastmoney_rows':len(b),'matched_rows':len(mm),'cninfo_match_rate':len(mm)/len(a) if len(a) else 0,'eastmoney_match_rate':len(mm)/len(b) if len(b) else 0,'cninfo_only':len(a)-len(mm),'eastmoney_only':len(b)-len(mm)})
    summ=pd.DataFrame(summaries); complete=bool(audit.loc[audit.page.eq('SUMMARY'),'complete'].fillna(False).all()) if len(audit) else False; pdf_rate=float(pdf.pdf_text_ok.mean()) if len(pdf) else 0.0
    result={'sample_codes':SAMPLE,'period':[args.start,args.end],'cninfo_rows':int(len(cn)),'eastmoney_rows':int(len(em)),'matched_rows':int(len(matches)),'pagination_complete':complete,'pdf_test_rows':int(len(pdf)),'pdf_text_ok':int(pdf.pdf_text_ok.sum()) if len(pdf) else 0,'pdf_text_success_rate':pdf_rate,'decision':('CNINFO_PRIMARY_READY' if complete and pdf_rate>=.90 and len(cn)>0 else 'NEEDS_MORE_WORK')}
    cn.to_csv(out/'cninfo_announcements.csv',index=False,encoding='utf-8-sig'); em.to_csv(out/'eastmoney_announcements.csv',index=False,encoding='utf-8-sig'); audit.to_csv(out/'cninfo_query_audit.csv',index=False,encoding='utf-8-sig'); matches.to_csv(out/'matched_announcements.csv',index=False,encoding='utf-8-sig'); cn_only.head(500).to_csv(out/'cninfo_only_sample.csv',index=False,encoding='utf-8-sig'); em_only.head(500).to_csv(out/'eastmoney_only_sample.csv',index=False,encoding='utf-8-sig'); pdf.to_csv(out/'cninfo_pdf_test.csv',index=False,encoding='utf-8-sig'); summ.to_csv(out/'coverage_summary.csv',index=False,encoding='utf-8-sig'); (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2)); print(summ.to_string(index=False))

if __name__=='__main__':main()
