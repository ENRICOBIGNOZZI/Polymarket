#!/usr/bin/env python3
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

YES='1001'; NO='1002'
STATE=sys.argv[2] if len(sys.argv)>2 else ''
def closed(): return bool(STATE and os.path.exists(STATE))
def market_obj(is_closed=False):
    return {'id':'m1','conditionId':'c1','slug':'mock-market','question':'Will mock event happen?',
            'liquidityNum':5000,'negRisk':False,'active':not is_closed,'closed':is_closed,'eventId':'e1',
            'clobTokenIds':json.dumps([YES,NO]),'outcomes':json.dumps(['Yes','No']),
            'outcomePrices':json.dumps(['1.0','0.0'] if is_closed else ['0.40','0.60']),
            'feesEnabled':False,'feeSchedule':{'exponent':1,'rate':0.0,'takerOnly':True}}
class H(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def _send(self,obj,status=200):
        raw=json.dumps(obj).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        p=urlparse(self.path)
        if p.path=='/markets': self._send([] if closed() else [market_obj(False)])
        elif p.path=='/markets/m1': self._send(market_obj(closed()))
        elif p.path=='/clob-markets/c1': self._send({'mos':1,'mts':0.01,'fd':{'r':0.0,'e':1,'to':True},'t':[{'t':YES,'o':'Yes'},{'t':NO,'o':'No'}]})
        elif p.path=='/fee-rate': self._send({'base_fee':0})
        else: self._send({'error':'not found'},404)
    def do_POST(self):
        if urlparse(self.path).path!='/books': return self._send({'error':'not found'},404)
        n=int(self.headers.get('Content-Length','0')); req=json.loads(self.rfile.read(n) or b'[]'); out=[]
        for x in req:
            t=str(x['token_id']); yes=t==YES
            out.append({'market':'c1','asset_id':t,'timestamp':'1','hash':'h','bids':[{'price':'0.39' if yes else '0.59','size':'5000'}],'asks':[{'price':'0.40' if yes else '0.60','size':'5000'}],'min_order_size':'1','tick_size':'0.01','neg_risk':False,'last_trade_price':'0.40' if yes else '0.60'})
        self._send(out)
if __name__=='__main__':
    port=int(sys.argv[1]); ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
