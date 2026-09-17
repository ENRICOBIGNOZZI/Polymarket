#!/usr/bin/env python3
import argparse, json, math, socket, subprocess, sys, time
from pathlib import Path
from urllib.request import Request, urlopen

p=argparse.ArgumentParser()
p.add_argument('--app', required=True)
p.add_argument('--region', required=True)
p.add_argument('--expected-sha', required=True)
p.add_argument('--duration', type=float, default=90.0)
p.add_argument('--handshakes', type=int, default=30)
p.add_argument('--output', required=True)
a=p.parse_args()
if len(a.expected_sha) != 40 or any(c not in '0123456789abcdef' for c in a.expected_sha):
    raise SystemExit('exact lowercase SHA required')
if not 5 <= a.duration <= 3600 or not 1 <= a.handshakes <= 1000:
    raise SystemExit('probe bounds invalid')
sys.path.insert(0, str(Path(a.app)/'scripts'))
from v7_public_book_wire_probe import WebSocket, WS_URL, discover_market, _events
head=subprocess.check_output(['git','-C',a.app,'rev-parse','HEAD'],text=True).strip()
if head != a.expected_sha: raise SystemExit(f'exact SHA mismatch: {head} != {a.expected_sha}')

def dist(xs):
    xs=sorted(xs)
    if not xs: return {}
    def q(v): return xs[max(0,min(len(xs)-1,math.ceil(v*len(xs))-1))]
    return {k:round(q(v),3) for k,v in [('p50',.5),('p90',.9),('p95',.95),('p99',.99),('p99_9',.999),('max',1.0)]}

def server_clock_sample():
    t0=time.time_ns()/1e6
    req=Request('https://clob.polymarket.com/time',headers={'User-Agent':'Polymarket-V7-latency-probe/1'})
    with urlopen(req,timeout=5) as r: server_ms=float(r.read().decode().strip())*1000.0
    t1=time.time_ns()/1e6
    return server_ms-(t0+t1)/2.0, t1-t0
clock_offsets=[]; clock_rtts=[]
for _ in range(7):
    try:
        o,r=server_clock_sample(); clock_offsets.append(o); clock_rtts.append(r)
    except Exception: pass
clock_offset_ms=sorted(clock_offsets)[len(clock_offsets)//2] if clock_offsets else 0.0

market=discover_market()
tokens=[str(x) for x in market['_tokens']]
sub=json.dumps({'assets_ids':tokens,'type':'market','custom_feature_enabled':True},separators=(',',':'))
hand=[]; handshake_errors=[]
for _ in range(max(1,a.handshakes)):
    t=time.monotonic_ns()
    try:
        w=WebSocket(WS_URL,10.0); hand.append((time.monotonic_ns()-t)/1e6); w.close()
    except Exception as e:
        handshake_errors.append(type(e).__name__+':'+str(e))

stream_hand=[]; first_book=[]; inter=[]; raw_ages=[]; corrected_ages=[]
message_count=0; book_count=0; bytes_total=0; eof_count=0; timeout_count=0
connect_failures=0; stream_connections=0; prev_mono=None; errors=[]
deadline=time.monotonic()+max(5.0,a.duration)
while time.monotonic() < deadline:
    w=None
    try:
        t=time.monotonic_ns(); w=WebSocket(WS_URL,10.0)
        stream_hand.append((time.monotonic_ns()-t)/1e6); stream_connections += 1
        send_mono=time.monotonic_ns(); w.send_text(sub); got_first=False
        while time.monotonic() < deadline:
            try: text=w.recv_message()
            except socket.timeout:
                timeout_count += 1; continue
            except EOFError:
                eof_count += 1; break
            if text is None: continue
            recv_mono=time.monotonic_ns(); recv_wall=time.time_ns()/1e6
            bytes_total += len(text.encode('utf-8','replace'))
            try: value=json.loads(text)
            except Exception: continue
            found=list(_events(value))
            if not found: continue
            message_count += 1
            if not got_first:
                first_book.append((recv_mono-send_mono)/1e6); got_first=True
            if prev_mono is not None: inter.append((recv_mono-prev_mono)/1e6)
            prev_mono=recv_mono
            for outer,book,_ in found:
                book_count += 1
                raw=book.get('timestamp') or outer.get('timestamp')
                try:
                    ts=float(raw)
                    if ts>1e12:
                        age=recv_wall-ts; raw_ages.append(age); corrected_ages.append(age+clock_offset_ms)
                except Exception: pass
    except Exception as e:
        connect_failures += 1; errors.append(type(e).__name__+':'+str(e))
    finally:
        if w is not None:
            try: w.close()
            except Exception: pass
    if time.monotonic() < deadline: time.sleep(0.05)

result={
  'schema':'pm_public_ws_latency_probe_v2','region':a.region,'exact_code_sha':a.expected_sha,
  'endpoint':WS_URL,'duration_s':a.duration,'market_slug':str(market.get('slug') or ''),
  'handshake_attempts':a.handshakes,'handshake_successes':len(hand),'handshake_failures':len(handshake_errors),
  'handshake_ms':dist(hand),'stream_handshake_ms':dist(stream_hand),
  'time_to_first_book_ms':dist(first_book),'stream_connections':stream_connections,
  'reconnect_count':max(0,stream_connections-1),'eof_count':eof_count,'socket_timeout_count':timeout_count,
  'stream_connect_failures':connect_failures,'book_messages':message_count,'book_events':book_count,'bytes':bytes_total,
  'book_message_interarrival_ms':dist(inter),'book_timestamp_age_raw_ms':dist(raw_ages),
  'book_timestamp_age_clock_corrected_ms':dist(corrected_ages),
  'clock_server_minus_local_ms':dist(clock_offsets),'clock_probe_rtt_ms':dist(clock_rtts),
  'clock_offset_median_used_ms':round(clock_offset_ms,3),'negative_corrected_age_count':sum(x<0 for x in corrected_ages),
  'handshake_errors':handshake_errors[:10],'stream_errors':errors[:10],
  'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
  'authorizes_live_execution':False,
  'measurement_scope':'PUBLIC_POLYMARKET_WEBSOCKET_CONNECTIVITY_ONLY',
  'clock_caveat':'/time has coarse second resolution; corrected frame age is directional, not one-way latency proof'
}
Path(a.output).write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
print(json.dumps(result,sort_keys=True))
