#!/usr/bin/env python3
"""Incremental research-only settlement labels for ALL submitted PAPER orders.

Run on a research-plane ledger mirror, not in the London hot path. SQLite state
survives restarts. No canonical ledger writes, capital changes or order actions.
Limit-price payoffs are diagnostics, never booked or presented as executable PnL.
"""
from __future__ import annotations
import argparse, hashlib, json, sqlite3, time
from pathlib import Path
from v7_crypto_cross_asset_audit import public_resolution

SCHEMA='v7_nonfill_outcomes_v1'

def connect(path: Path):
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY,v TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,digest TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,market TEXT NOT NULL,token TEXT NOT NULL,body TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS resolutions(market TEXT PRIMARY KEY,body TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS states(id TEXT PRIMARY KEY,body TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS filled(id TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE IF NOT EXISTS resolution_attempts(market TEXT PRIMARY KEY,checked REAL NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS orders_market_idx ON orders(market)')
    db.commit();return db

def ingest(db, ledger: Path):
    st=ledger.stat();key=str(ledger.resolve());saved=db.execute('SELECT v FROM state WHERE k=?',(key,)).fetchone()
    cursor=json.loads(saved[0]) if saved else {'device':st.st_dev,'inode':st.st_ino,'offset':0}
    if cursor['device']!=st.st_dev or cursor['inode']!=st.st_ino or st.st_size<cursor['offset']:
        # Research mirror atomic replacement: replay; record identities deduplicate.
        cursor={'device':st.st_dev,'inode':st.st_ino,'offset':0}
    count=0
    try:
        with ledger.open('rb') as f:
            f.seek(cursor['offset'])
            while f.tell()<st.st_size:
                start=f.tell();line=f.readline()
                if not line.endswith(b'\n') or f.tell()>st.st_size:break
                r=json.loads(line);identity=r.get('record_id')
                if not identity:raise ValueError('missing record identity')
                digest=hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                old=db.execute('SELECT digest FROM events WHERE id=?',(identity,)).fetchone()
                if old and old[0]!=digest:raise ValueError('conflicting record identity')
                if not old:
                    if r.get('paper_only') is not True or r.get('authenticated_execution') is not False:
                        raise ValueError('not a PAPER ledger')
                    md=r.get('metadata') or {};oid=r.get('order_id');kind=r.get('event_type')
                    if kind=='ORDER_SUBMITTED':
                        if db.execute('SELECT 1 FROM orders WHERE id=?',(oid,)).fetchone():raise ValueError('duplicate order submission')
                        db.execute('INSERT INTO orders VALUES(?,?,?,?)',(oid,str(r['market_id']),str(r['token_id']),json.dumps(r)))
                    elif kind=='ORDER_STATE':db.execute('INSERT OR REPLACE INTO states VALUES(?,?)',(oid,json.dumps(r)))
                    elif kind=='FILL':db.execute('INSERT OR IGNORE INTO filled VALUES(?)',(oid,))
                    elif kind=='FINAL' and md.get('settlement_payouts'):
                        resolved={'market_id':str(r['market_id']),'resolved':True,'payouts':md['settlement_payouts'],
                           'source':'CANONICAL_FINAL','observed_at_unix':r['recorded_ts_ms']/1000}
                        db.execute('INSERT OR REPLACE INTO resolutions VALUES(?,?)',(str(r['market_id']),json.dumps(resolved)))
                    db.execute('INSERT INTO events VALUES(?,?)',(identity,digest));count+=1
                cursor['offset']=f.tell()
        db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(key,json.dumps(cursor)));db.commit()
    except Exception:
        db.rollback();raise
    return count

def label(db, cache: Path, maximum_markets: int=30):
    cache.mkdir(parents=True,exist_ok=True)
    missing=[r[0] for r in db.execute('''SELECT DISTINCT o.market FROM orders o LEFT JOIN resolution_attempts a ON o.market=a.market WHERE o.market NOT IN (SELECT market FROM resolutions) ORDER BY COALESCE(a.checked,0),o.market LIMIT ?''',(maximum_markets,))]
    found=0
    for market in missing:
        result=public_resolution(market,cache)
        db.execute('INSERT OR REPLACE INTO resolution_attempts VALUES(?,?)',(market,time.time()))
        if result.get('resolved'):
            db.execute('INSERT OR REPLACE INTO resolutions VALUES(?,?)',(market,json.dumps(result)));found+=1
    db.commit();return found

def export(db, output: Path):
    output.parent.mkdir(parents=True,exist_ok=True);tmp=output.with_suffix(output.suffix+'.tmp')
    n=resolved=filled=0
    query='''SELECT o.id,o.market,o.token,o.body,r.body,s.body,f.id
             FROM orders o LEFT JOIN resolutions r ON o.market=r.market
             LEFT JOIN states s ON o.id=s.id LEFT JOIN filled f ON o.id=f.id ORDER BY o.id'''
    with tmp.open('w') as out:
        for oid,market,token,body,resolution,state,fill in db.execute(query):
            r=json.loads(body);label=json.loads(resolution) if resolution else {};s=json.loads(state) if state else {}
            y=(label.get('payouts') or {}).get(token);price=r.get('limit_price');q=r.get('intended_size')
            value={'schema':SCHEMA,'order_id':oid,'market_id':market,'token_id':token,
              'asset':(r.get('metadata') or {}).get('asset'),'horizon':(r.get('metadata') or {}).get('horizon'),
              'decision_ts_ms':r.get('decision_ts_ms'),'paper_only':True,'research_evidence_only':True,
              'counterfactual':True,'counterfactual_execution_verified':False,'canonical_pnl':None,
              'was_filled':fill is not None,'execution_reason':(s.get('metadata') or {}).get('paper_execution_reason'),
              'execution_censored':(s.get('metadata') or {}).get('execution_observation_censored'),
              'settlement_payoff':y,'label_source':label.get('source'),'label_observed_at_unix':label.get('observed_at_unix'),
              'hypothetical_gross_at_limit':float(q)*(float(y)-float(price)) if y is not None and q is not None and price is not None else None,
              'hypothetical_net_at_limit':None,'costs_complete':False,
              'warning':'NOT_EXECUTABLE_PNL; mutually exclusive retries must not be added as a strategy.'}
            out.write(json.dumps(value,allow_nan=False)+'\n');n+=1;resolved+=y is not None;filled+=fill is not None
    tmp.replace(output)
    return {'orders':n,'resolved_orders':resolved,'filled_orders':filled,'pending_labels':n-resolved}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--ledger',type=Path,required=True);p.add_argument('--database',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--cache',type=Path,required=True);p.add_argument('--fetch-public',action='store_true');a=p.parse_args()
    with connect(a.database) as db:
        new=ingest(db,a.ledger)
        if a.fetch_public:label(db,a.cache)
        print(json.dumps({'new_records':new,**export(db,a.output),'canonical_ledger_modified':False}))
