#!/usr/bin/env python3
"""Bounded public CLOB batch-book helper for zero-authority research collectors."""
from __future__ import annotations
import json,math,urllib.request
from typing import Any,Iterable

def fetch_books(
    base:str,tokens:Iterable[str],timeout:float,*,chunk_size:int=50,
    user_agent:str="polymarket-v7-batch-books",
)->dict[str,dict[str,Any]]:
    unique=list(dict.fromkeys(str(x) for x in tokens if str(x)))
    out={}
    for i in range(0,len(unique),max(1,chunk_size)):
        chunk=unique[i:i+max(1,chunk_size)]
        body=json.dumps([{"token_id":token} for token in chunk],separators=(",",":")).encode()
        req=urllib.request.Request(
            base.rstrip("/")+"/books",data=body,method="POST",
            headers={"Content-Type":"application/json","User-Agent":user_agent})
        try:
            with urllib.request.urlopen(req,timeout=timeout) as resp:value=json.load(resp)
        except (OSError,TimeoutError,json.JSONDecodeError):
            continue
        if not isinstance(value,list):continue
        for row in value:
            if not isinstance(row,dict):continue
            token=str(row.get("asset_id") or "")
            if token:out[token]=row
    return out

def _levels(rows:Any,reverse:bool)->list[tuple[float,float]]:
    out=[]
    for row in rows if isinstance(rows,list) else []:
        if not isinstance(row,dict):continue
        try:p=float(row["price"]);q=float(row["size"])
        except (KeyError,TypeError,ValueError,OverflowError):continue
        if math.isfinite(p) and math.isfinite(q) and 0<p<1 and q>0:out.append((p,q))
    return sorted(out,key=lambda x:x[0],reverse=reverse)

def full_book(row:dict[str,Any]|None)->dict[str,list[tuple[float,float]]]|None:
    if not isinstance(row,dict):return None
    bids=_levels(row.get("bids"),True);asks=_levels(row.get("asks"),False)
    return {"bids":bids,"asks":asks} if bids and asks else None

def bbo(row:dict[str,Any]|None)->dict[str,float]|None:
    book=full_book(row)
    if book is None:return None
    bid=book["bids"][0];ask=book["asks"][0]
    if not bid[0]<ask[0]:return None
    return {"bid":bid[0],"bid_q":bid[1],"ask":ask[0],"ask_q":ask[1]}
