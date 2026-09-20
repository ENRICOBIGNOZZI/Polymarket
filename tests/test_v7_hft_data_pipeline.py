import gzip
import json
from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'monitoring'))
sys.path.insert(0,str(ROOT/'ops'))
from v7_hft_windows import Windows, SECOND, HEADER, RAW, merged, contains
from v7_hft_data_health import compare
from v7_hft_readonly_rsync import sender_args


def native(second,kind=2,reason=6,accepted=False,signal=1):
    return dict(schema='polymarket_v7_native_observation_v1',paper_only=True,execution_authority=False,
                server_id='london',run_id='run',capture_id='capture',asset='BTC',horizon='M5',
                market_id='market',token_id='yes',kind=kind,sequence=second,
                decision_wall_ns=second*SECOND,observed_monotonic_ns=second*SECOND,
                close_wall_ns=300*SECOND,close_monotonic_ns=300*SECOND,
                signal_version=signal,trigger_monotonic_ns=second*SECOND-1,
                signal_valid=True,reason=reason,accepted=accepted)


def write_native(root,rows,closed=True):
    p=root/'research/native_observations/run/market-capture.jsonl';p.parent.mkdir(parents=True,exist_ok=True)
    p.write_bytes(b''.join(json.dumps(r).encode()+b'\n' for r in rows))
    if closed:
        Path(str(p)+'.closed.json').write_text(json.dumps(dict(closed=True,healthy=True,bytes=p.stat().st_size)))
    return p


def test_rejected_tte_expired_and_accepted_survive_once(tmp_path):
    rows=[native(10,reason=6),native(11,reason=3,signal=2),native(12,accepted=True,signal=3),native(13,kind=6)]
    p=write_native(tmp_path,rows);store=Windows(tmp_path)
    try:
        first=store.ingest();assert first['total_opportunities']==3
        again=store.ingest();assert again['decisions']==0
        proof=store.preserve(p)
        assert proof['window_records']==4
        assert gzip.decompress((store.root/proof['window']).read_bytes())==p.read_bytes()
        (store.root/proof['window']).unlink()
        with pytest.raises(FileNotFoundError):store.preserve(p)
    finally:store.close()


def test_active_and_partial_native_prefix_cannot_authorize_retirement(tmp_path):
    p=write_native(tmp_path,[native(10),native(11)],closed=False);store=Windows(tmp_path)
    try:
        store.ingest(max_rows=1)
        assert not list(store.db.execute('SELECT * FROM watermarks'))
        with pytest.raises(ValueError,match='ACTIVE_NATIVE'):store.preserve(p)
        Path(str(p)+'.closed.json').write_text(json.dumps(dict(closed=True,healthy=True)))
        with pytest.raises(ValueError,match='PREFIX_INCOMPLETE'):store.preserve(p)
        store.ingest();assert store.preserve(p)['source_records']==2
    finally:store.close()


def test_epoch_skips_old_training_but_keeps_new_pending_population(tmp_path):
    write_native(tmp_path,[native(10),native(20)])
    store=Windows(tmp_path,15*SECOND)
    try:
        assert store.ingest()['total_opportunities']==1
        rows=[json.loads(line) for p in (store.root/'compact').glob('*.jsonl.gz') for line in gzip.open(p,'rt')]
        assert len(rows)==1 and rows[0]['decision_wall_ns']==20*SECOND
        assert 'outcome' not in rows[0]  # No fabricated loss/zero settlement.
    finally:store.close()


def test_external_union_preserves_original_bytes_and_requires_population_watermark(tmp_path):
    store=Windows(tmp_path)
    try:
        p=tmp_path/'external_fair/raw/feed.segment-000001.bin';p.parent.mkdir(parents=True)
        header=HEADER.pack(b'PMV7RAW!',3,0,1,b'a'*40,b'run',b'session',b'binance')
        records=[RAW.pack(i,1,i*SECOND,i*SECOND,1,3)+b'abc' for i in range(5,21)]
        p.write_bytes(header+b''.join(records))
        with store.db:
            store.db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?)',('one','c','BTC','M5','m',10*SECOND,8*SECOND,15*SECOND,6,0))
        with pytest.raises(ValueError,match='WATERMARKS'):store.preserve(p)
        with store.db:
            store.db.executemany('INSERT INTO watermarks VALUES (?,?,?)',[('BTC',h,30*SECOND) for h in ('M5','M15','H1','H4','D1')])
        proof=store.preserve(p)
        assert proof['window_records']==8
        assert gzip.decompress((store.root/proof['window']).read_bytes())==header+b''.join(records[3:11])
    finally:store.close()


def test_sender_refuses_shell_upload_path_escape_and_link_dereference(tmp_path):
    root=tmp_path/'research/hft_permanent'
    good=f'rsync --server --sender -tr . {root}/'
    assert sender_args(good,root)[-1]==str(root)+'/'
    for command in ('sh',good+'; cat /etc/passwd',good.replace('--sender ',''),
                    good.replace('-tr','-trL'),good.replace(str(root),'/etc')):
        with pytest.raises(ValueError):sender_args(command,root)


def test_growth_measurement_does_not_count_compression_as_new_events():
    before=dict(at_ns=SECOND,files={'old':[100,'cex_raw',False,SECOND]})
    after=dict(at_ns=31*SECOND,files={'old':[200,'cex_raw',False,2*SECOND],
               'gzip':[90,'cex_raw',True,2*SECOND]},category_bytes={},total_bytes=290)
    report=compare(before,after)
    assert report['gb_per_hour']['cex_raw']==pytest.approx(100/30*3600/1e9)
    assert merged([(1,5),(4,7),(10,11)])==[[1,7],[10,11]]
    assert contains([[1,7],[10,11]],7) and not contains([[1,7],[10,11]],8)


def test_one_time_epoch_cleanup_excludes_current_open_recent_models_and_changed_files(tmp_path):
    import os
    from v7_prune_pre_epoch import plan,execute
    active=tmp_path/'paper_v7_london_live';active.mkdir()
    archive=tmp_path/'paper_v7_london_archives'
    paths=[]
    for name in ('old','open','recent','model','changed'):
        p=archive/'external_fair/raw'/(name+'.bin');p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(b'data');os.utime(p,ns=(1,1));paths.append(p)
    old,opened,recent,model,changed=paths;os.utime(recent,ns=(100,100))
    current=active/'external_fair/raw/current.bin';current.parent.mkdir(parents=True);current.write_bytes(b'current');os.utime(current,ns=(1,1))
    opened_ids={(opened.stat().st_dev,opened.stat().st_ino)}
    rows=plan(active,50,opened_ids)
    assert {Path(r['path']).name for r in rows}=={'old.bin','changed.bin'}
    changed.write_bytes(b'new data')
    result=execute(rows,active,50,opened_ids)
    assert result['removed_files']==1 and not old.exists()
    assert all(p.exists() for p in (opened,recent,model,changed,current))
