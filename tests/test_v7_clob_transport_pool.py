from __future__ import annotations
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from test_v7_clob_tls import _openssl_flags

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = r'''
#include "pm/v7_clob_transport_pool.hpp"
#include <array>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string_view>
#include <thread>
using namespace pm::v7::clob;
static long long now(){return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
int main(int argc,char**argv){
 DualPersistentTlsTransport p("localhost",(unsigned short)std::atoi(argv[1]),2000);
 auto c=p.connect(argv[2]); assert(c.ready && p.ready());
 std::atomic<long long> order_done{0},cancel_done{0}; std::atomic<bool> go{false};
 auto call=[&](TransportLane lane,std::string_view path,std::atomic<long long>& done){
  while(!go.load(std::memory_order_acquire)) std::this_thread::yield();
  std::array<char,128> req{}; auto n=std::snprintf(req.data(),req.size(),"GET %.*s HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n",(int)path.size(),path.data());
  assert(p.lane(lane).write_all({req.data(),(std::size_t)n}).ok); std::array<char,256>b{}; assert(p.lane(lane).read_some(b).ok); done.store(now(),std::memory_order_release);
 };
 std::thread a(call,TransportLane::Order,"/order",std::ref(order_done));
 std::thread b(call,TransportLane::Cancel,"/cancel",std::ref(cancel_done));
 go.store(true,std::memory_order_release); a.join(); b.join();
 assert(cancel_done.load() < order_done.load()); p.close(); return 0;
}
'''

def test_order_stall_does_not_block_cancel_lane() -> None:
    cxx = shutil.which("c++")
    assert cxx
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        cert, key = p / "cert.pem", p / "key.pem"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-keyout",str(key),"-out",str(cert),"-days","1","-subj","/CN=localhost","-addext","subjectAltName=DNS:localhost"],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        listener=socket.socket(); listener.bind(("127.0.0.1",0)); listener.settimeout(10.0); listener.listen(2); port=listener.getsockname()[1]
        errors=[]; accepted=[]
        def server():
            try:
                ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert,key); ctx.set_alpn_protocols(["http/1.1"])
                threads=[]
                def handle(raw):
                    with ctx.wrap_socket(raw,server_side=True) as conn:
                        data=b""
                        while b"\r\n\r\n" not in data: data+=conn.recv(4096)
                        if data.startswith(b"GET /order "): time.sleep(.35)
                        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nConnection: keep-alive\r\n\r\nx")
                for _ in range(2):
                    raw,_=listener.accept(); raw.settimeout(5.0); accepted.append(1); t=threading.Thread(target=handle,args=(raw,)); t.start(); threads.append(t)
                for t in threads: t.join()
            except BaseException as e: errors.append(e)
            finally: listener.close()
        st=threading.Thread(target=server)
        src=p/"main.cpp"; exe=p/"pool"; src.write_text(PROGRAM)
        inc, libs = _openssl_flags()
        subprocess.run([cxx,"-std=c++20","-O2",f"-I{ROOT/'include'}",*inc,str(ROOT/"src/v7_clob_tls.cpp"),str(src),"-o",str(exe),*libs],check=True)
        st.start()
        subprocess.run([str(exe),str(port),str(cert)],check=True,timeout=10); st.join(timeout=5)
        assert not st.is_alive() and not errors and len(accepted)==2
