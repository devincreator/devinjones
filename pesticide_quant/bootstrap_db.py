#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse, sqlite3
from pathlib import Path

COMPANIES = [(1, 'CN.SH.600486', '600486', '扬农化工', 'SH', '沪市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (2, 'CN.SH.603360', '603360', '百傲化学', 'SH', '沪市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (3, 'CN.SZ.301035', '301035', '润丰股份', 'SZ', '创业板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (4, 'CN.SH.600596', '600596', '新安股份', 'SH', '沪市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (5, 'CN.SZ.002250', '002250', '联化科技', 'SZ', '深市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (6, 'CN.SZ.301665', '301665', '泰禾股份', 'SZ', '创业板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (7, 'CN.SZ.000553', '000553', '安道麦A', 'SZ', '深市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (8, 'CN.SZ.002258', '002258', '利尔化学', 'SZ', '深市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (9, 'CN.SH.600389', '600389', '江山股份', 'SH', '沪市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (10, 'CN.SH.603599', '603599', '广信股份', 'SH', '沪市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (11, 'CN.SZ.002004', '002004', '华邦健康', 'SZ', '深市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (12, 'CN.SZ.002734', '002734', '利民股份', 'SZ', '深市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (13, 'CN.SZ.000525', '000525', '红太阳', 'SZ', '深市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (14, 'CN.SZ.300261', '300261', '雅本化学', 'SZ', '创业板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (15, 'CN.SZ.002749', '002749', '国光股份', 'SZ', '深市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (16, 'CN.BJ.920819', '920819', '颖泰生物', 'BJ', '北交所', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (17, 'CN.SH.603639', '603639', '海利尔', 'SH', '沪市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (18, 'CN.SH.600731', '600731', '湖南海利', 'SH', '沪市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (19, 'CN.SZ.002391', '002391', '长青股份', 'SZ', '深市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (20, 'CN.SH.603585', '603585', '苏利股份', 'SH', '沪市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (21, 'CN.SH.603086', '603086', '先达股份', 'SH', '沪市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (22, 'CN.SH.603970', '603970', '中农立华', 'SH', '沪市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (23, 'CN.SZ.300796', '300796', '贝斯美', 'SZ', '创业板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (24, 'CN.SZ.002942', '002942', '新农股份', 'SZ', '深市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (25, 'CN.SZ.002496', '002496', '*ST辉丰', 'SZ', '深市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'ST', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (26, 'CN.SZ.300575', '300575', '中旗股份', 'SZ', '创业板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (27, 'CN.SH.605033', '605033', '美邦股份', 'SH', '沪市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (28, 'CN.SZ.300804', '300804', '广康生化', 'SZ', '创业板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (29, 'CN.SH.603810', '603810', '丰山集团', 'SH', '沪市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (30, 'CN.SZ.002513', '002513', '蓝丰生化', 'SZ', '深市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (31, 'CN.SZ.001231', '001231', '农心科技', 'SZ', '深市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (32, 'CN.SZ.003042', '003042', '中农联合', 'SZ', '深市主板', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21'), (33, 'CN.BJ.920866', '920866', '绿亨科技', 'BJ', '北交所', '化工', '化学制品', '农药', '220303', 1, '2026-01-21', 'Normal', 'https://www.lixinger.com/equity/industry/detail/sw/220303/220303/constituents/list', '2026-01-21')]
ALIASES = [('920819', '833819', 'BJ', '2020-07-27', '2025-05-05', 'https://www.bse.cn/service/code_mapping.html', '颖泰生物：2025-05-06起启用920819；此前北交所/精选层历史代码833819。'), ('920819', '920819', 'BJ', '2025-05-06', None, 'https://www.bse.cn/service/code_mapping.html', '颖泰生物当前代码。'), ('920866', '870866', 'BJ', '2022-12-09', '2025-10-08', 'https://www.bse.cn/service/code_mapping.html', '绿亨科技：2025-10-09起北交所存量股票统一切换920号段；此前代码870866。'), ('920866', '920866', 'BJ', '2025-10-09', None, 'https://www.bse.cn/service/code_mapping.html', '绿亨科技当前代码。')]

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS company_master (
 seq INTEGER, company_id TEXT PRIMARY KEY, code TEXT NOT NULL, name TEXT NOT NULL, exchange TEXT NOT NULL,
 board TEXT, sw_l1 TEXT, sw_l2 TEXT, sw_l3 TEXT, sw_code TEXT, current_member INTEGER, snapshot_date TEXT,
 status_name TEXT, source_url TEXT, source_asof TEXT);
CREATE TABLE IF NOT EXISTS industry_membership (
 code TEXT NOT NULL, sw_code TEXT NOT NULL, sw_name TEXT NOT NULL, snapshot_date TEXT NOT NULL,
 is_member INTEGER NOT NULL, source_id TEXT, PRIMARY KEY(code,sw_code,snapshot_date));
CREATE TABLE IF NOT EXISTS code_alias_history (
 canonical_code TEXT NOT NULL, historical_code TEXT NOT NULL, exchange TEXT NOT NULL,
 valid_from TEXT, valid_to TEXT, source_url TEXT, note TEXT,
 PRIMARY KEY (canonical_code, historical_code, valid_from));
CREATE TABLE IF NOT EXISTS source_registry (
 source_id TEXT PRIMARY KEY,data_layer TEXT,source_name TEXT,url TEXT,coverage TEXT,source_type TEXT,
 reliability TEXT,v1_status TEXT,notes TEXT);
CREATE TABLE IF NOT EXISTS ingestion_job (
 job_id TEXT PRIMARY KEY, layer TEXT NOT NULL, source_id TEXT NOT NULL, status TEXT NOT NULL,
 target_entities INTEGER, loaded_entities INTEGER DEFAULT 0, loaded_rows INTEGER DEFAULT 0,
 min_date TEXT, max_date TEXT, last_attempt_ts TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS data_quality_issue (
 issue_id INTEGER PRIMARY KEY AUTOINCREMENT, layer TEXT, code TEXT, issue_type TEXT, issue_date TEXT,
 severity TEXT, details TEXT, created_ts TEXT);
CREATE TABLE IF NOT EXISTS market_daily (
 trade_date TEXT NOT NULL,code TEXT NOT NULL,open_raw REAL,high_raw REAL,low_raw REAL,close_raw REAL,
 prev_close_raw REAL,pct_chg_pct REAL,volume_shares REAL,amount_cny REAL,turnover_pct REAL,adj_factor REAL,
 close_qfq REAL,is_trade INTEGER,is_st INTEGER,limit_status TEXT,source_id TEXT,ingest_ts TEXT,
 open_qfq REAL, high_qfq REAL, low_qfq REAL, amplitude_pct REAL, change_amt REAL,
 PRIMARY KEY(trade_date,code));
CREATE TABLE IF NOT EXISTS financial_quarterly (
 code TEXT NOT NULL,report_period TEXT NOT NULL,report_type TEXT,ann_date TEXT NOT NULL,available_date TEXT NOT NULL,
 revision_id TEXT DEFAULT '0',revenue_cny REAL,revenue_yoy_pct REAL,net_profit_parent_cny REAL,net_profit_yoy_pct REAL,
 deduct_np_cny REAL,roe_weighted_pct REAL,gross_margin_pct REAL,net_margin_pct REAL,cfo_cny REAL,total_assets_cny REAL,
 total_liab_cny REAL,equity_parent_cny REAL,inventory_cny REAL,ar_cny REAL,capex_cny REAL,source_id TEXT,ingest_ts TEXT,
 update_date TEXT, source_asof TEXT,
 PRIMARY KEY(code,report_period,ann_date,revision_id));
CREATE TABLE IF NOT EXISTS capital_flow_daily (
 trade_date TEXT NOT NULL,code TEXT NOT NULL,margin_balance_cny REAL,margin_buy_cny REAL,margin_repay_cny REAL,
 short_balance_cny REAL,source_id TEXT,ingest_ts TEXT,PRIMARY KEY(trade_date,code));
CREATE TABLE IF NOT EXISTS industry_product_daily (
 date TEXT NOT NULL,product_id TEXT NOT NULL,product_name TEXT,category TEXT,price REAL,unit TEXT,region TEXT,
 frequency TEXT,source_id TEXT,ingest_ts TEXT,PRIMARY KEY(date,product_id,region));
CREATE TABLE IF NOT EXISTS feature_daily (
 feature_date TEXT NOT NULL,code TEXT NOT NULL,feature_version TEXT NOT NULL,feature_name TEXT NOT NULL,
 feature_value REAL,source_cutoff_date TEXT,PRIMARY KEY(feature_date,code,feature_version,feature_name));
CREATE TABLE IF NOT EXISTS label_daily (
 label_date TEXT NOT NULL,code TEXT NOT NULL,label_version TEXT NOT NULL,fwd_ret_20d REAL,fwd_ret_40d REAL,
 fwd_ret_60d REAL,max_drawdown_60d REAL,max_upside_60d REAL,risk_label INTEGER,opportunity_label INTEGER,
 PRIMARY KEY(label_date,code,label_version));
CREATE TABLE IF NOT EXISTS industry_membership_history (
 code TEXT NOT NULL, source_code TEXT NOT NULL, sw_name TEXT NOT NULL DEFAULT '农药',
 sw_index_code TEXT NOT NULL DEFAULT '850333', source_industry_code TEXT, source_industry_name TEXT,
 classification_version TEXT, in_date TEXT NOT NULL, out_date TEXT, source_update_time TEXT,
 source_id TEXT NOT NULL, loaded_at TEXT NOT NULL,
 PRIMARY KEY (code, in_date, source_industry_code, source_id));
CREATE TABLE IF NOT EXISTS industry_membership_source_raw (
 source_code TEXT NOT NULL, canonical_code TEXT NOT NULL, start_date TEXT, industry_code TEXT,
 industry_name TEXT, update_time TEXT, source_id TEXT NOT NULL, raw_json TEXT, loaded_at TEXT NOT NULL);
"""

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",required=True)
    args=ap.parse_args()
    p=Path(args.db); p.parent.mkdir(parents=True,exist_ok=True)
    if p.exists(): p.unlink()
    con=sqlite3.connect(p)
    con.executescript(SCHEMA)
    con.executemany("""INSERT INTO company_master VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", COMPANIES)
    con.executemany("""INSERT INTO code_alias_history VALUES (?,?,?,?,?,?,?)""", ALIASES)
    con.execute("""INSERT OR IGNORE INTO industry_membership(code,sw_code,sw_name,snapshot_date,is_member,source_id)
                   SELECT code,sw_code,sw_l3,snapshot_date,1,'S001' FROM company_master""")
    con.executemany("""INSERT OR REPLACE INTO ingestion_job
       (job_id,layer,source_id,status,target_entities,loaded_entities,loaded_rows,note)
       VALUES(?,?,?,?,?,?,?,?)""", [
       ("J001","market_daily","S009","PIPELINE_READY",33,0,0,"GitHub Actions market pipeline"),
       ("J002","financial_quarterly","S010","PIPELINE_READY",33,0,0,"GitHub Actions PIT finance pipeline"),
       ("J003","industry_membership_history","S013","PIPELINE_READY",None,0,0,"Official SW historical membership required"),
    ])
    con.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",[
       ("database_version","github-actions-bootstrap-v1"),
       ("snapshot_date","2026-01-21"),
       ("formal_universe","SW pesticide level-3 PIT"),
    ])
    con.commit()
    print("company_master", con.execute("select count(*) from company_master").fetchone()[0])
    print("aliases", con.execute("select count(*) from code_alias_history").fetchone()[0])
    con.close()

if __name__=="__main__":
    main()
