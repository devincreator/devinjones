#!/usr/bin/env python3
import argparse, json, pathlib, subprocess, tempfile

ARTS = [
    # content API failed in V2
    'AN202006091383575241',
    'AN202006091383581592',
    'AN202006091383581598',
    'AN202006101383908799',
    'AN202006091383581601',
    # content API succeeded in V2
    'AN202005181379914133',
    'AN202005181379896625',
    'AN202005181379910132',
]

def run(art):
    url=f'https://pdf.dfcfw.com/pdf/H2_{art}_1.pdf'
    with tempfile.TemporaryDirectory() as td:
        pdf=pathlib.Path(td)/'a.pdf'; txt=pathlib.Path(td)/'a.txt'
        cp=subprocess.run(['curl','-L','--retry','3','--connect-timeout','10','--max-time','30','-sS','-w','%{http_code}|%{size_download}|%{content_type}',url,'-o',str(pdf)],text=True,capture_output=True)
        meta=cp.stdout.strip(); err=cp.stderr.strip()
        code=size=ctype=''
        if '|' in meta:
            parts=meta.split('|',2); code,size,ctype=parts
        text_chars=0; text_head=''; pdf_ok=pdf.exists() and pdf.stat().st_size>500
        if pdf_ok:
            pp=subprocess.run(['pdftotext','-layout',str(pdf),str(txt)],text=True,capture_output=True)
            if txt.exists():
                t=txt.read_text(encoding='utf-8',errors='ignore').strip(); text_chars=len(t); text_head=t[:120].replace('\n',' ')
        return {'art_code':art,'url':url,'http_code':code,'bytes':int(size or 0),'content_type':ctype,'curl_error':err,'pdf_ok':bool(pdf_ok),'text_chars':text_chars,'text_head':text_head}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--out',default='pdf_probe.json'); a=ap.parse_args()
    rows=[run(x) for x in ARTS]
    out={'rows':rows,'http_200':sum(r['http_code']=='200' for r in rows),'text_ok':sum(r['text_chars']>100 for r in rows)}
    pathlib.Path(a.out).write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(out,ensure_ascii=False,indent=2))
    if out['http_200'] < 5: raise SystemExit(2)
if __name__=='__main__': main()
