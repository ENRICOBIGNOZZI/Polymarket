#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,math,os,threading,time,urllib.error,urllib.parse,urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from typing import Any

END={"","LTE=","-1"}

def f(x:Any,d:float=0.0)->float:
    try:y=float(x)
    except (TypeError,ValueError,OverflowError):return d
    return y if math.isfinite(y) else d

def b(x:Any,d:bool=False)->bool:
    if isinstance(x,bool):return x
    if isinstance(x,(int,float)):return bool(x)
    if isinstance(x,str):return x.strip().lower() in {"1","true","yes"}
    return d

def atomic(path:Path,obj:Any)->None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(obj,indent=2,sort_keys=True)+"\n",encoding="utf-8"); os.replace(tmp,path)

def req(url:str,payload:Any|None=None,timeout:float=6.0,attempts:int=2)->Any:
    data=None if payload is None else json.dumps(payload,separators=(",",":")).encode(); hdr={"User-Agent":"polymarket-v6-market-proxy/1","Accept":"application/json"}
    if data is not None:hdr["Content-Type"]="application/json"
    last:Exception|None=None
    for i in range(max(1,attempts)):
        try:
            r=urllib.request.Request(url,data=data,headers=hdr,method="POST" if data is not None else "GET")
            with urllib.request.urlopen(r,timeout=timeout) as h:return json.loads(h.read().decode())
        except (OSError,TimeoutError,urllib.error.URLError,urllib.error.HTTPError,json.JSONDecodeError) as e:
            last=e
            if i+1<attempts:time.sleep(.25*(2**i))
    raise RuntimeError(f"request failed: {url}: {last}")

def tokens(m:dict[str,Any])->list[dict[str,Any]]:
    x=m.get("tokens");return [z for z in x if isinstance(z,dict)] if isinstance(x,list) else []

def depth(book:dict[str,Any])->float:
    bids=book.get("bids") if isinstance(book.get("bids"),list) else []; asks=book.get("asks") if isinstance(book.get("asks"),list) else []
    if not bids or not asks:return 0.0
    def side(rows:list[Any])->float:
        out=0.0
        for z in rows[:5]:
            if isinstance(z,dict):
                p,q=f(z.get("price"),-1),f(z.get("size"),0)
                if 0<p<1 and q>0:out+=p*q
        return out
    return min(side(bids),side(asks))

class Proxy:
    def __init__(self,gamma:str,clob:str,cache:Path,status:Path):
        self.gamma=gamma.rstrip('/');self.clob=clob.rstrip('/');self.cache=cache;self.status=status;self.lock=threading.RLock();self.rows:list[dict[str,Any]]=[];self.ts=0.0;self.idmap:dict[str,str]={};self.exact:dict[str,tuple[float,Any]]={};self.failures=0;self.error="";self.source="startup";self.load()
    def load(self)->None:
        try:x=json.loads(self.cache.read_text())
        except (OSError,json.JSONDecodeError):return
        if isinstance(x,dict):
            if isinstance(x.get("markets"),list):self.rows=[z for z in x["markets"] if isinstance(z,dict)];self.ts=f(x.get("timestamp"),0)
            if isinstance(x.get("gamma_to_condition"),dict):self.idmap={str(k):str(v) for k,v in x["gamma_to_condition"].items() if k and v}
    def stat(self,source:str,n:int,up:bool,age:float=0)->None:
        self.source=source;atomic(self.status,{"schema":"polymarket_v6_market_proxy_status_v1","timestamp":int(time.time()),"source":source,"markets":n,"upstream_gamma_ok":up,"failures":self.failures,"last_error":self.error,"cache_age_seconds":max(0.0,age),"paper_only":True})
    def save(self,rows:list[dict[str,Any]])->None:
        self.rows=rows;self.ts=time.time()
        for z in rows:
            mid,cid=str(z.get("id") or ""),str(z.get("conditionId") or "")
            if mid and cid:self.idmap[mid]=cid
        atomic(self.cache,{"schema":"polymarket_v6_market_proxy_cache_v1","timestamp":int(self.ts),"markets":rows,"gamma_to_condition":self.idmap})
    def gamma_rows(self,n:int,q:dict[str,list[str]])->list[dict[str,Any]]:
        params={k:v[-1] for k,v in q.items() if v and k in {"active","closed","order","ascending","liquidity_num_min"}};params.setdefault("active","true");params.setdefault("closed","false")
        cur="";out=[];seen=set()
        for _ in range(30):
            if len(out)>=n:break
            p=dict(params);p["limit"]=str(min(100,n-len(out)))
            if cur:p["after_cursor"]=cur
            x=req(self.gamma+"/markets/keyset?"+urllib.parse.urlencode(p),timeout=5,attempts=2)
            if not isinstance(x,dict) or not isinstance(x.get("markets"),list):raise RuntimeError("bad Gamma keyset response")
            batch=[z for z in x["markets"] if isinstance(z,dict)]
            for z in batch:
                key=str(z.get("id") or z.get("conditionId") or "")
                if key and key not in seen:seen.add(key);out.append(z)
            nxt=str(x.get("next_cursor") or "")
            if not batch or nxt in END or nxt==cur:break
            cur=nxt
        if not out:raise RuntimeError("Gamma keyset returned no markets")
        return out[:n]
    def clob_candidates(self,n:int)->list[dict[str,Any]]:
        out=[];cur=""
        for _ in range(20):
            if len(out)>=n:break
            url=self.clob+"/markets"+("?"+urllib.parse.urlencode({"next_cursor":cur}) if cur else "")
            x=req(url,timeout=6,attempts=3)
            if not isinstance(x,dict) or not isinstance(x.get("data"),list):raise RuntimeError("bad CLOB markets response")
            batch=[z for z in x["data"] if isinstance(z,dict)]
            for z in batch:
                if not b(z.get("active"),True) or b(z.get("closed")) or b(z.get("archived")) or not b(z.get("accepting_orders"),True):continue
                if z.get("enable_order_book") is not None and not b(z.get("enable_order_book"),True):continue
                if str(z.get("condition_id") or "") and len(tokens(z))>=2:out.append(z)
                if len(out)>=n:break
            nxt=str(x.get("next_cursor") or "")
            if not batch or nxt in END or nxt==cur:break
            cur=nxt
        return out
    def books(self,cand:list[dict[str,Any]])->dict[str,dict[str,Any]]:
        ids=[str(t.get("token_id") or "") for z in cand for t in tokens(z) if str(t.get("token_id") or "")];out={}
        for i in range(0,len(ids),80):
            x=req(self.clob+"/books",[{"token_id":t} for t in ids[i:i+80]],timeout=6,attempts=2)
            if isinstance(x,list):
                for z in x:
                    if isinstance(z,dict) and str(z.get("asset_id") or ""):out[str(z["asset_id"])]=z
        return out
    def convert(self,z:dict[str,Any],books:dict[str,dict[str,Any]])->dict[str,Any]|None:
        cid=str(z.get("condition_id") or "");tt=tokens(z);ids=[str(t.get("token_id") or "") for t in tt];outs=[str(t.get("outcome") or "") for t in tt]
        if not cid or len(ids)<2 or not ids[0] or not ids[1]:return None
        liq=min(depth(books.get(ids[0],{})),depth(books.get(ids[1],{})))
        if liq<=0:return None
        return {"id":cid,"conditionId":cid,"eventId":cid,"slug":str(z.get("market_slug") or z.get("slug") or cid),"question":str(z.get("question") or ""),"liquidityNum":liq,"volume24hr":0.0,"negRisk":b(z.get("neg_risk")),"active":b(z.get("active"),True),"closed":b(z.get("closed")),"enableOrderBook":b(z.get("enable_order_book"),True),"acceptingOrders":b(z.get("accepting_orders"),True),"clobTokenIds":ids,"outcomes":outs,"events":[],"_proxy_source":"clob"}
    def clob_rows(self,n:int,minliq:float)->list[dict[str,Any]]:
        cand=self.clob_candidates(max(300,min(2500,n*2)));books=self.books(cand);out=[]
        for z in cand:
            x=self.convert(z,books)
            if x and f(x.get("liquidityNum"))>=minliq:out.append(x)
        out.sort(key=lambda z:f(z.get("liquidityNum")),reverse=True);return out[:n]
    def markets(self,q:dict[str,list[str]])->list[dict[str,Any]]:
        n=max(1,min(2000,int(f((q.get("limit") or [100])[-1],100))));minliq=f((q.get("liquidity_num_min") or [0])[-1],0);now=time.time()
        with self.lock:
            if self.rows and now-self.ts<=20:
                r=[z for z in self.rows if f(z.get("liquidityNum"))>=minliq]
                if r:self.stat(self.source+"_cache",min(n,len(r)),self.source.startswith("gamma"),now-self.ts);return r[:n]
            try:r=self.gamma_rows(n,q);self.error="";self.save(r);self.stat("gamma_keyset",len(r),True);return r
            except Exception as e:self.failures+=1;self.error=str(e)
            try:
                r=self.clob_rows(n,minliq)
                if not r:raise RuntimeError("CLOB fallback found no two-sided liquid markets")
                self.save(r);self.stat("clob_fallback",len(r),False);return r
            except Exception as e:self.failures+=1;self.error=f"{self.error}; {e}"
            age=now-self.ts if self.ts else 1e12
            if self.rows and age<=900:
                r=[z for z in self.rows if f(z.get("liquidityNum"))>=minliq]
                if r:self.stat("stale_cache",min(n,len(r)),False,age);return r[:n]
            self.stat("unavailable",0,False,age);raise RuntimeError(self.error or "discovery unavailable")
    def one(self,mid:str)->dict[str,Any]:
        path="/markets/"+urllib.parse.quote(mid,safe="")
        try:
            x=req(self.gamma+path,timeout=5,attempts=2)
            if isinstance(x,dict):
                cid=str(x.get("conditionId") or "");gm=str(x.get("id") or mid)
                if cid:self.idmap[gm]=cid
                self.exact[path]=(time.time(),x);return x
        except Exception as e:self.failures+=1;self.error=str(e)
        c=self.exact.get(path)
        if c and time.time()-c[0]<=900 and isinstance(c[1],dict):return c[1]
        cid=self.idmap.get(mid,mid);x=req(self.clob+"/markets/"+urllib.parse.quote(cid,safe=""),timeout=6,attempts=2)
        if not isinstance(x,dict):raise RuntimeError("bad CLOB single market")
        y=self.convert(x,self.books([x]))
        if not y:raise RuntimeError("CLOB single market has no valid two-sided book")
        return y
    def generic(self,pathq:str)->Any:
        try:x=req(self.gamma+pathq,timeout=5,attempts=2);self.exact[pathq]=(time.time(),x);return x
        except Exception as e:self.failures+=1;self.error=str(e)
        c=self.exact.get(pathq)
        if c and time.time()-c[0]<=900:return c[1]
        raise RuntimeError(self.error or "Gamma unavailable")

class H(BaseHTTPRequestHandler):
    proxy:Proxy
    def log_message(self,*_:Any)->None:return
    def sendj(self,code:int,obj:Any)->None:
        raw=json.dumps(obj,separators=(",",":")).encode();self.send_response(code);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(raw)));self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(raw)
    def do_GET(self)->None:
        u=urllib.parse.urlsplit(self.path)
        if u.path=="/healthz":self.sendj(200,{"ok":True,"source":self.proxy.source,"failures":self.proxy.failures});return
        try:
            if u.path in {"/markets","/markets/keyset"}:
                rows=self.proxy.markets(urllib.parse.parse_qs(u.query,keep_blank_values=True));self.sendj(200,{"markets":rows,"next_cursor":""} if u.path.endswith("keyset") else rows);return
            if u.path.startswith("/markets/") and u.path.count("/")==2:self.sendj(200,self.proxy.one(urllib.parse.unquote(u.path.rsplit("/",1)[-1])));return
            self.sendj(200,self.proxy.generic(self.path))
        except Exception as e:self.sendj(503,{"error":"market_proxy_unavailable","detail":str(e)[:400]})

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--host",default="127.0.0.1");ap.add_argument("--port",type=int,default=9120);ap.add_argument("--gamma",default="https://gamma-api.polymarket.com");ap.add_argument("--clob",default="https://clob.polymarket.com");ap.add_argument("--cache",type=Path,required=True);ap.add_argument("--status",type=Path,required=True);a=ap.parse_args()
    H.proxy=Proxy(a.gamma,a.clob,a.cache,a.status);srv=ThreadingHTTPServer((a.host,a.port),H);print(f"v6 market proxy listening on http://{a.host}:{a.port}",flush=True)
    try:srv.serve_forever()
    except KeyboardInterrupt:pass
    finally:srv.server_close()
    return 0
if __name__=="__main__":raise SystemExit(main())
