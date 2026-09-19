"""Regressions for producer decision-window identity and ordinary book events."""
import pytest
from test_v7_native_repricing_dataset import capture, m, row, rows, write


def test_repeated_decisions_for_one_signal_do_not_conflict_or_share_labels(tmp_path):
    p=tmp_path/'x.jsonl';data=rows()
    later=row(observed=1_020_000_000)
    later.update(decision_monotonic_ns=1_020_000_000,reason=16,
                 yes_bid_e4=3900,yes_ask_e4=4100,no_bid_e4=5900,no_ask_e4=6100)
    data.insert(1,later);capture(p,data)
    out,s=m.build([p],require_closed=True)
    assert s['origins']==2 and len(out)==4 and s['censored_horizons']==4
    assert all(r['decision_monotonic_ns']==1_000_000_000 for r in out)


def test_ordinary_book_records_are_not_malformed_label_origins(tmp_path):
    p=tmp_path/'x.jsonl';data=rows()
    book=row(kind=1,observed=1_020_000_000)
    book['repricing_origin_signal_version']=None
    data.insert(1,book);capture(p,data)
    out,s=m.build([p],require_closed=True)
    assert len(out)==4 and s['invalid_records']==0


def test_closed_capture_requires_observed_connection_epoch(tmp_path):
    p=tmp_path/'x.jsonl';data=capture(p)
    for r in data:r.pop('connection_epoch')
    write(p,data)
    with pytest.raises(ValueError,match='invalid integer'):m.build([p],require_closed=True)
