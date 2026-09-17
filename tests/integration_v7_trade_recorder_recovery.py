"""A missing crypto universe fails closed; the same recorder then recovers."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time

SHA = "a" * 40


def run(binary):
    class Handler(BaseHTTPRequestHandler):
        healthy = False
        def do_GET(self):
            if not self.healthy:
                self.send_response(503); self.end_headers(); self.wfile.write(b'unavailable'); return
            self.send_response(200); self.end_headers(); self.wfile.write(b'[]')
        def log_message(self,*args): pass

    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory); url=f'http://127.0.0.1:{server.server_port}'
        universe=root/'universe/current.json'; universe.parent.mkdir(parents=True)
        process=subprocess.Popen([str(binary),'--run-dir',str(root),'--universe',str(universe),
                                  '--model-sha',SHA,'--data-url',url,'--interval','1','--loop'],
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        def wait(predicate):
            end=time.monotonic()+8
            while time.monotonic()<end:
                assert process.poll() is None, 'recorder exited during recoverable failure'
                try: status=json.loads((root/'trade_recorder_status.json').read_text())
                except (OSError,ValueError): status={}
                if predicate(status): return status
                time.sleep(.02)
            raise AssertionError('recorder did not publish expected health state')
        try:
            failed=wait(lambda s:s.get('errors')==1)
            assert failed['data_plane_healthy'] is False
            now=int(time.time()*1000)
            universe.write_text(json.dumps({
                'schema':'polymarket_v7_crypto_universe_snapshot_v1','timestamp_ms':now,
                'model_sha':SHA,'paper_only':True,'authenticated_execution':False,
                'real_order_submission':False,'execution_authority':False,
                'markets':[{'market_id':'1','condition_id':'c1'}],
            }))
            Handler.healthy=True
            recovered=wait(lambda s:s.get('data_plane_healthy') is True)
            assert recovered['flow_regime']=='CRYPTO_CLOB_NO_MATCHING_TRADES'
            assert recovered['scope']=='CONFIGURED_CRYPTO_CONTEXTS_ONLY'
            assert recovered['model_sha']==SHA and recovered['last_trade_ts']==0
        finally:
            process.terminate(); process.wait(timeout=3)
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--binary',type=Path,required=True)
    run(parser.parse_args().binary.resolve())
