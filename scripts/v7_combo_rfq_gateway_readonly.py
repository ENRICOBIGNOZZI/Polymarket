#!/usr/bin/env python3
"""Authenticated read-only Combo RFQ gateway collector.

The only application text frame this process can send is the initial auth
message. It has no implementation for maker quote, quote cancel, or last-look
response. Received RFQ events are appended to a zero-authority evidence tape.
"""
from __future__ import annotations
import argparse,base64,hashlib,json,os,secrets,socket,ssl,struct,time
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

STATUS_SCHEMA="polymarket_v7_combo_rfq_gateway_status_v1"
ALLOWED_RECEIVE={
    "auth","RFQ_REQUEST","RFQ_CONFIRMATION_REQUEST","RFQ_EXECUTION_UPDATE",
    "RFQ_TRADE","RFQ_ERROR","ACK_RFQ_QUOTE","ACK_RFQ_QUOTE_CANCEL",
    "ACK_RFQ_CONFIRMATION_RESPONSE",
}

def _read_exact(sock:ssl.SSLSocket,n:int)->bytes:
    out=bytearray()
    while len(out)<n:
        chunk=sock.recv(n-len(out))
        if not chunk:raise ConnectionError("eof")
        out.extend(chunk)
    return bytes(out)

def _connect(url:str,timeout:float)->ssl.SSLSocket:
    u=urlparse(url)
    if u.scheme!="wss" or not u.hostname:raise ValueError("wss required")
    port=u.port or 443;path=u.path or "/"
    if u.query:path+="?"+u.query
    raw=socket.create_connection((u.hostname,port),timeout=timeout)
    ctx=ssl.create_default_context()
    sock=ctx.wrap_socket(raw,server_hostname=u.hostname)
    key=base64.b64encode(secrets.token_bytes(16)).decode()
    request=(
        f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode()
    sock.sendall(request)
    buf=b""
    while b"\r\n\r\n" not in buf:
        buf+=sock.recv(4096)
        if len(buf)>16384:raise ConnectionError("handshake overflow")
    head=buf.split(b"\r\n\r\n",1)[0].decode("latin1")
    if not head.startswith("HTTP/1.1 101"):raise ConnectionError("upgrade rejected")
    headers={}
    for line in head.split("\r\n")[1:]:
        if ":" in line:
            k,v=line.split(":",1);headers[k.strip().lower()]=v.strip()
    want=base64.b64encode(hashlib.sha1(
        (key+"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    if headers.get("sec-websocket-accept")!=want:raise ConnectionError("bad accept")
    sock.settimeout(timeout)
    return sock

def _send_frame(sock:ssl.SSLSocket,payload:bytes,opcode:int)->None:
    if len(payload)>=126:raise ValueError("outbound frame too large")
    mask=secrets.token_bytes(4)
    header=bytes([0x80|opcode,0x80|len(payload)])+mask
    body=bytes(x^mask[i%4] for i,x in enumerate(payload))
    sock.sendall(header+body)

def _send_auth(sock:ssl.SSLSocket,auth:dict[str,Any])->None:
    body=json.dumps(auth,separators=(",",":")).encode()
    _send_frame(sock,body,0x1)

def _recv_frame(sock:ssl.SSLSocket,max_bytes:int)->tuple[int,bytes]:
    h=_read_exact(sock,2);opcode=h[0]&0x0f;masked=(h[1]&0x80)!=0;length=h[1]&0x7f
    if length==126:length=struct.unpack("!H",_read_exact(sock,2))[0]
    elif length==127:length=struct.unpack("!Q",_read_exact(sock,8))[0]
    if length>max_bytes:raise ValueError("frame too large")
    mask=_read_exact(sock,4) if masked else b""
    data=_read_exact(sock,length)
    if masked:data=bytes(x^mask[i%4] for i,x in enumerate(data))
    return opcode,data

def atomic(path:Path,value:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8");os.replace(tmp,path)

def creds()->dict[str,str]|None:
    names={
        "apiKey":"PM_V7_COMBO_API_KEY","secret":"PM_V7_COMBO_API_SECRET",
        "passphrase":"PM_V7_COMBO_API_PASSPHRASE",
        "signer_address":"PM_V7_COMBO_SIGNER_ADDRESS",
        "maker_address":"PM_V7_COMBO_MAKER_ADDRESS",
    }
    out={k:os.environ.get(v,"").strip() for k,v in names.items()}
    return out if all(out.values()) else None

def run(args:argparse.Namespace)->None:
    c=creds()
    if c is None:
        atomic(args.status,{"schema":STATUS_SCHEMA,"state":"DISABLED_NO_READONLY_CREDENTIALS",
            "paper_only":True,"authenticated_data_access":False,
            "authenticated_execution":False,"real_order_submission":False,
            "model_sha":args.model_sha,"timestamp_ms":time.time_ns()//1_000_000})
        time.sleep(args.retry_seconds);return
    sock=_connect(args.url,args.timeout_seconds)
    auth={"type":"auth","auth":{"apiKey":c["apiKey"],"secret":c["secret"],
          "passphrase":c["passphrase"]},"identity":{
          "signer_address":c["signer_address"],"maker_address":c["maker_address"],
          "signature_type":args.signature_type}}
    _send_auth(sock,auth)
    received=0;last_ms=time.time_ns()//1_000_000
    try:
        while True:
            opcode,data=_recv_frame(sock,args.maximum_frame_bytes)
            now=time.time_ns()//1_000_000
            if opcode==0x8:return
            if opcode==0x9:
                _send_frame(sock,data,0xA);continue
            if opcode==0xA:continue
            if opcode!=0x1:continue
            try:event=json.loads(data.decode())
            except (UnicodeDecodeError,json.JSONDecodeError):continue
            if not isinstance(event,dict):continue
            typ=str(event.get("type") or "")
            if typ not in ALLOWED_RECEIVE:continue
            event["receive_wall_ms"]=now
            event["model_sha"]=args.model_sha
            event["paper_only"]=True
            event["authenticated_execution"]=False
            event["real_order_submission"]=False
            with args.tape.open("a",encoding="utf-8") as h:
                h.write(json.dumps(event,sort_keys=True,separators=(",",":"))+"\n")
            received+=1;last_ms=now
            atomic(args.status,{"schema":STATUS_SCHEMA,"state":"COLLECTING_READ_ONLY",
                "paper_only":True,"authenticated_data_access":True,
                "authenticated_execution":False,"real_order_submission":False,
                "outbound_application_messages":"AUTH_ONLY",
                "model_sha":args.model_sha,"timestamp_ms":now,
                "received_messages":received,"last_receive_wall_ms":last_ms})
    finally:
        try:sock.close()
        except OSError:pass

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--tape",type=Path,required=True)
    ap.add_argument("--status",type=Path,required=True)
    ap.add_argument("--url",default="wss://combos-rfq-gateway-quoter.polymarket.com/ws/rfq")
    ap.add_argument("--signature-type",type=int,default=0)
    ap.add_argument("--timeout-seconds",type=float,default=10.0)
    ap.add_argument("--retry-seconds",type=float,default=5.0)
    ap.add_argument("--maximum-frame-bytes",type=int,default=1_048_576)
    args=ap.parse_args()
    args.tape.parent.mkdir(parents=True,exist_ok=True);args.tape.touch(exist_ok=True)
    args.status.parent.mkdir(parents=True,exist_ok=True)
    while True:
        try:run(args)
        except Exception as exc:
            atomic(args.status,{"schema":STATUS_SCHEMA,"state":"DISCONNECTED",
                "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
                "model_sha":args.model_sha,"timestamp_ms":time.time_ns()//1_000_000,
                "error":type(exc).__name__})
            time.sleep(args.retry_seconds)

if __name__=="__main__":raise SystemExit(main())
