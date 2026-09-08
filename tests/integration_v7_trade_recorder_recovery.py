"""A discovery outage invalidates evidence; the same recorder then recovers."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time


def run(binary):
    class Handler(BaseHTTPRequestHandler):
        healthy = False

        def do_GET(self):
            if not self.healthy:
                self.send_response(503); self.end_headers(); self.wfile.write(b'unavailable'); return
            self.send_response(200); self.end_headers()
            market={'id':'1','conditionId':'c1','active':True,'closed':False,
                    'liquidityNum':100.,'clobTokenIds':['y1','n1'],'outcomes':['Yes','No']}
            body={'markets':[market],'next_cursor':''} if self.path.startswith('/markets') else []
            self.wfile.write(json.dumps(body).encode())

        def log_message(self,*args): pass

    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory); url=f'http://127.0.0.1:{server.server_port}'
        config=root/'paper_v7.json'; config.write_text(json.dumps({'gamma_url':url,'paper_only':True}))
        process=subprocess.Popen([str(binary),'--config',str(config),'--run-dir',str(root),
                                  '--data-url',url,'--interval','1','--loop'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        def wait(predicate):
            end=time.monotonic()+8
            while time.monotonic()<end:
                assert process.poll() is None, 'recorder exited during discovery failure'
                try: status=json.loads((root/'trade_recorder_status.json').read_text())
                except (OSError,ValueError): status={}
                if predicate(status): return status
                time.sleep(.02)
            raise AssertionError('recorder did not publish expected health state')
        try:
            failed=wait(lambda s:s.get('errors')==1)
            assert failed['data_plane_healthy'] is False
            Handler.healthy=True
            recovered=wait(lambda s:s.get('data_plane_healthy') is True)
            assert recovered['flow_regime']=='STANDARD_CLOB_NO_MATCHING_TRADES'
            assert recovered['last_trade_ts']==0
        finally:
            process.terminate(); process.wait(timeout=3)
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--binary',type=Path,required=True)
    run(parser.parse_args().binary.resolve())
