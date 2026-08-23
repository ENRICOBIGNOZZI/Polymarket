#!/usr/bin/env python3
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

YES='1001'; NO='1002'; TRADE=sys.argv[2]

def market():
    return {'id':'m1','conditionId':'c1','slug':'maker-mock','question':'Will maker mock happen?',
            'liquidityNum':5000,'volume24hr':1000,'negRisk':False,'active':True,'closed':False,
            'enableOrderBook':True,'acceptingOrders':True,'eventId':'e1',
            'clobTokenIds':json.dumps([YES,NO]),'outcomes':json.dumps(['Yes','No']),
            'outcomePrices':json.dumps(['0.395','0.605']),'feesEnabled':False,
            'feeSchedule':{'exponent':1,'rate':0.0,'takerOnly':True}}

class H(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def sendj(self,obj,status=200):
        raw=json.dumps(obj).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        p=urlparse(self.path)
        if p.path=='/markets': return self.sendj([market()])
        if p.path=='/markets/m1': return self.sendj(market())
        if p.path=='/clob-markets/c1': return self.sendj({'mos':1,'mts':0.01,'fd':{'r':0.0,'e':1,'to':True},'t':[{'t':YES,'o':'Yes'},{'t':NO,'o':'No'}]})
        if p.path=='/fee-rate': return self.sendj({'base_fee':0})
        if p.path=='/prices-history': return self.sendj({'history':[{'t':int(time.time())-120,'p':0.39},{'t':int(time.time())-60,'p':0.395}]})
        if p.path=='/trades':
            if not os.path.exists(TRADE): return self.sendj([])
            return self.sendj([{'proxyWallet':'0x0000000000000000000000000000000000000000','side':'SELL','asset':YES,'conditionId':'c1','size':10000,'price':0.39,'timestamp':int(time.time()),'title':'maker mock','slug':'maker-mock','eventSlug':'maker-mock','outcome':'Yes','outcomeIndex':0}])
        return self.sendj({'error':'not found'},404)
    def do_POST(self):
        p=urlparse(self.path)
        n=int(self.headers.get('Content-Length','0')); raw=self.rfile.read(n) or b'[]'
        if p.path=='/books':
            req=json.loads(raw); out=[]
            for x in req:
                t=str(x['token_id']); yes=t==YES
                out.append({'market':'c1','asset_id':t,'timestamp':str(int(time.time())),'hash':'h',
                            'bids':[{'price':'0.39' if yes else '0.59','size':'5000'}],
                            'asks':[{'price':'0.40' if yes else '0.60','size':'5000'}],
                            'min_order_size':'1','tick_size':'0.01','neg_risk':False,
                            'last_trade_price':'0.395' if yes else '0.605'})
            return self.sendj(out)
        if p.path=='/batch-prices-history':
            req=json.loads(raw); now=int(time.time()); hist={str(t):[{'t':now-120,'p':0.39 if str(t)==YES else 0.61},{'t':now-60,'p':0.395 if str(t)==YES else 0.605}] for t in req.get('markets',[])}
            return self.sendj({'history':hist})
        return self.sendj({'error':'not found'},404)

if __name__=='__main__':
    ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),H).serve_forever()
