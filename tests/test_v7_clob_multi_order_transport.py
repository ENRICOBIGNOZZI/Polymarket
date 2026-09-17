from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = r'''
#include "pm/v7_clob_multi_order_transport.hpp"
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
 MultiOrderPersistentTlsTransport<4> p("localhost",(unsigned short)std::atoi(argv[1]),2000);
 auto c=p.connect(argv[2]); assert(c.ready && p.ready() && p.order_lane_count()==4);
 assert(&p.order_lane_for(5)==&p.order_lane(1));
 std::array<std::atomic<long long>,4> done{}; std::atomic<long long> cancel_done{0}; std::atomic<bool> go{false};
 auto order=[&](int i){ while(!go.load(std::memory_order_acquire)) std::this_thread::yield();
  std::array<char,128> req{}; int n=std::snprintf(req.data(),req.size(),"GET /order%d HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n",i);
  assert(p.order_lane((std::size_t)i).write_all({req.data(),(std::size_t)n}).ok); std::array<char,256>b{}; assert(p.order_lane((std::size_t)i).read_some(b).ok); done[(std::size_t)i].store(now(),std::memory_order_release); };
 auto cancel=[&]{ while(!go.load(std::memory_order_acquire)) std::this_thread::yield();
  constexpr std::string_view r="GET /cancel HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n";
  assert(p.cancel_lane().write_all({r.data(),r.size()}).ok); std::array<char,256>b{}; assert(p.cancel_lane().read_some(b).ok); cancel_done.store(now(),std::memory_order_release); };
 std::array<std::thread,4> workers{std::thread(order,0),std::thread(order,1),std::thread(order,2),std::thread(order,3)}; std::thread canceller(cancel);
 go.store(true,std::memory_order_release); for(auto& t:workers)t.join(); canceller.join();
 const auto slow=done[0].load(); assert(cancel_done.load()<slow); for(int i=1;i<4;++i) assert(done[(std::size_t)i].load()<slow);
 auto* preferred=&p.order_lane(1); assert(p.try_connected_order_lane_for(1)==preferred); preferred->close();
 auto* failover=p.try_connected_order_lane_for(1); assert(failover!=nullptr && failover!=preferred && failover->connected());
 p.close(); assert(p.try_connected_order_lane_for(1)==nullptr); return 0;
}
'''

def test_independent_order_lanes_remove_response_hol() -> None:
    cxx = shutil.which("c++")
    assert cxx
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        cert, key = p / "cert.pem", p / "key.pem"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-keyout",str(key),"-out",str(cert),"-days","1","-subj","/CN=localhost","-addext","subjectAltName=DNS:localhost"],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        listener=socket.socket(); listener.bind(("127.0.0.1",0)); listener.listen(5); port=listener.getsockname()[1]
        errors=[]; accepted=[]
        def server():
            try:
                ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert,key); ctx.set_alpn_protocols(["http/1.1"])
                threads=[]
                def handle(raw):
                    with ctx.wrap_socket(raw,server_side=True) as conn:
                        data=b""
                        while b"\r\n\r\n" not in data: data+=conn.recv(4096)
                        if data.startswith(b"GET /order0 "): time.sleep(.30)
                        elif data.startswith(b"GET /cancel "): time.sleep(.01)
                        else: time.sleep(.03)
                        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nConnection: keep-alive\r\n\r\nx")
                for _ in range(5):
                    raw,_=listener.accept(); accepted.append(1); t=threading.Thread(target=handle,args=(raw,)); t.start(); threads.append(t)
                for t in threads: t.join()
            except BaseException as e: errors.append(e)
            finally: listener.close()
        st=threading.Thread(target=server); st.start()
        src=p/"main.cpp"; exe=p/"multi-order"; src.write_text(PROGRAM)
        prefix=subprocess.check_output(["brew","--prefix","openssl@3"],text=True).strip()
        subprocess.run([cxx,"-std=c++20","-O2",f"-I{ROOT/'include'}",f"-I{prefix}/include",str(ROOT/"src/v7_clob_tls.cpp"),str(src),"-o",str(exe),f"-L{prefix}/lib","-lssl","-lcrypto"],check=True)
        subprocess.run([str(exe),str(port),str(cert)],check=True,timeout=10); st.join(timeout=5)
        assert not st.is_alive() and not errors and len(accepted)==5
