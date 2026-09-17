from __future__ import annotations
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from v7_file_event import AtomicReplaceWatcher

def atomic(path:Path,text:str)->None:
    tmp=path.with_name(path.name+'.tmp');tmp.write_text(text);os.replace(tmp,path)

def test_atomic_replace_is_observed_and_idle_timeout_is_false():
    with tempfile.TemporaryDirectory() as d:
        p=Path(d)/'signal.json';atomic(p,'0')
        with AtomicReplaceWatcher(p) as watcher:
            thread=threading.Thread(target=lambda:(time.sleep(.03),atomic(p,'1')));thread.start()
            assert watcher.wait(.3)
            thread.join()
            assert not watcher.wait(.01)

def test_unix_datagram_json_delivers_latest_signal():
    import json, socket
    from v7_file_event import UnixDatagramJsonReceiver
    with tempfile.TemporaryDirectory() as d:
        p=Path(d)/'signal.sock'; receiver=UnixDatagramJsonReceiver(p)
        sender=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
        sender.sendto(json.dumps({'signal_version':1}).encode(),str(p))
        sender.sendto(json.dumps({'signal_version':2}).encode(),str(p))
        value=receiver.wait_json(.2)
        try:
            assert value['signal_version']==2
            assert receiver.received==2 and receiver.invalid==0
        finally:
            sender.close();receiver.close()
            assert not p.exists()
def _run_direct() -> None:
    tests = [(name, value) for name, value in globals().items()
             if name.startswith("test_") and callable(value)]
    for test_name, test in sorted(tests):
        test()
    print(f"{len(tests)} direct tests passed")


if __name__ == "__main__":
    _run_direct()
