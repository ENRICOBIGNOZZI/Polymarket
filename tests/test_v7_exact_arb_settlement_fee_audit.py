from copy import deepcopy
import sys
from pathlib import Path
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import v7_exact_arb_settlement_fee_audit as audit


def hx(n): return f"0x{n:064x}"
def data(values): return "0x"+"".join(f"{n:064x}" for n in values)


def fixture():
    exchange=audit.EXCHANGES[0]
    block=dict(hash=hx(888),number="0x1234",timestamp="0x1000")
    base=dict(address=exchange,blockHash=block["hash"],blockNumber=block["number"],
              removed=False,transactionHash=hx(999),transactionIndex="0x1")
    rows=[
        dict(**base,logIndex="0x1",topics=[audit.FILLED,hx(1),hx(111),hx(222)],data=data([1,100,5000000,2650000,0,0,0])),
        dict(**base,logIndex="0x2",topics=[audit.FILLED,hx(2),hx(222),hx(int(exchange,16))],data=data([0,100,2650000,5000000,87180,0,0])),
        dict(**base,logIndex="0x3",topics=[audit.MATCHED,hx(2),hx(222)],data=data([0,100,2650000,5000000])),
    ]
    return block,rows


def transport(block,rows):
    def read(url,request):
        assert url==audit.RPC_URLS[0]
        method=request["method"]
        if method=="eth_chainId": result="0x89"
        elif method in {"eth_getBlockByNumber","eth_getBlockByHash"}: result=block
        elif method=="eth_getLogs":
            assert request["params"]==[{"blockHash":block["hash"],"address":list(audit.EXCHANGES)}]
            result=rows
        else: raise AssertionError("not_readonly")
        return audit.canonical(dict(jsonrpc="2.0",id=request["id"],result=result))
    return read


def test_floor_and_half_up_are_distinguished_without_verifying_a_rate():
    block,rows=fixture();receipt=audit.collect(block["hash"],transport(block,rows))
    result=audit.validate(receipt);row=result["observations"][0]
    assert result["taker_matches"]==result["positive_fee_matches"]==1
    assert row["maker_amounts_reconciled"]
    rates=row["hypotheses"]["7/100"]
    assert rates["fee_micro"]["FLOOR_AGGREGATE_VWAP"]==87180
    assert rates["fee_micro"]["HALF_UP_AGGREGATE_VWAP"]==87190
    assert all(name.startswith("FLOOR") for name in rates["matching_models"])
    assert not row["historical_rate_verified"] and not row["rounding_mode_verified"]
    assert not result["venue_execution_verified"] and receipt["valid_until"] is None
    assert len(receipt["records"])==5


def test_multilevel_aggregation_is_not_per_fill_rounding():
    block,rows=fixture();rows[0]["data"]=data([1,100,1000000,520000,0,0,0])
    extra=deepcopy(rows[0]);extra["logIndex"]="0x2";extra["topics"][1]=hx(3)
    extra["data"]=data([1,100,1000000,540000,0,0,0])
    rows[1].update(logIndex="0x3",data=data([0,100,1060000,2000000,34870,0,0]))
    rows[2].update(logIndex="0x4",data=data([0,100,1060000,2000000]));rows.insert(1,extra)
    row=audit.analyze(block,rows)["observations"][0]
    assert row["maker_amounts_reconciled"] and row["maker_count"]==2
    models=row["hypotheses"]["7/100"]["fee_micro"]
    assert models["FLOOR_AGGREGATE_VWAP"]==34870
    assert models["FLOOR_PER_MAKER_FILL"]==34850
    assert models["HALF_UP_PER_PRICE_LEVEL"]==34860


def test_zero_fee_does_not_attest_zero_rate():
    block,rows=fixture();rows[1]["data"]=data([0,100,2650000,5000000,0,0,0])
    result=audit.analyze(block,rows)
    assert result["positive_fee_matches"]==0 and result["positive_fee_hypothesis_matches"]=={}
    assert result["observations"][0]["hypotheses"]["0"]["matching_models"]
    assert result["historical_rate_verified"] is False


@pytest.mark.parametrize("mutation,reason",[
    (lambda b,r:r[0].update(removed=True),"removed_or_invalid_log"),
    (lambda b,r:r[0].update(blockHash=hx(42)),"log_block_or_exchange"),
    (lambda b,r:r[1].update(logIndex=r[0]["logIndex"]),"duplicate_or_unordered_log"),
    (lambda b,r:r[2]["topics"].__setitem__(1,hx(55)),"taker_matched_binding"),
    (lambda b,r:r[0]["topics"].__setitem__(3,hx(333)),"maker_taker_binding"),
    (lambda b,r:r[0].update(data=data([1,101,5000000,2650000,0,0,0])),"complementary_token_binding"),
    (lambda b,r:r.pop(),"unmatched_fill_tail"),
    (lambda b,r:r.pop(0),"matched_without_complete_fill_group"),
    (lambda b,r:r[0].update(data=data([2,100,5000000,2650000,0,0,0])),"fill_amounts"),
])
def test_source_defects_fail_closed(mutation,reason):
    block,rows=fixture();mutation(block,rows)
    receipt=audit.collect(block["hash"],transport(block,rows))
    assert receipt["state"]=="SOURCE_OR_BINDING_ERROR" and receipt["error"]==reason
    assert receipt["proof_material"] is None
    with pytest.raises(ValueError,match="receipt_authority"): audit.validate(receipt)


@pytest.mark.parametrize("mutation",[
    lambda r:r.update(venue_execution_verified=True),lambda r:r.update(real_order_submission=True),
    lambda r:r["proof_material"].update(positive_fee_matches=500),
    lambda r:r["records"][3]["request"].update(method="eth_sendRawTransaction"),
    lambda r:r["records"].pop(),
])
def test_rehashed_claims_and_transcripts_are_revalidated(mutation):
    block,rows=fixture();receipt=audit.collect(block["hash"],transport(block,rows))
    mutation(receipt);receipt["proof_hash"]=audit.sha({k:v for k,v in receipt.items() if k!="proof_hash"})
    with pytest.raises(ValueError): audit.validate(receipt)


def test_source_timeout_does_not_retain_success():
    def unavailable(*args): raise TimeoutError("provider_timeout")
    receipt=audit.collect(hx(888),unavailable)
    assert receipt["state"]=="SOURCE_OR_BINDING_ERROR" and receipt["proof_material"] is None


def test_nonreconciled_amounts_cannot_invent_fragmentation_evidence():
    block,rows=fixture();rows[0]["data"]=data([1,100,4999999,2650000,0,0,0])
    row=audit.analyze(block,rows)["observations"][0]
    assert not row["maker_amounts_reconciled"]
    assert set(row["hypotheses"]["7/100"]["fee_micro"])=={"FLOOR_AGGREGATE_VWAP","HALF_UP_AGGREGATE_VWAP"}


@pytest.mark.parametrize("side,same_side",[(0,False),(0,True),(1,False),(1,True)])
def test_buy_sell_complementary_split_merge_amount_reconstruction(side,same_side):
    block,rows=fixture()
    q,c=5000000,2650000
    maker_side=side if same_side else 1-side
    maker_token=101 if same_side else 100
    if same_side: maker_making,maker_taking=(q-c,q) if side==0 else (q,q-c)
    else: maker_making,maker_taking=(q,c) if side==0 else (c,q)
    making,taking=(c,q) if side==0 else (q,c)
    rows[0]["data"]=data([maker_side,maker_token,maker_making,maker_taking,0,0,0])
    rows[1]["data"]=data([side,100,making,taking,87180,0,0])
    rows[2]["data"]=data([side,100,making,taking])
    row=audit.analyze(block,rows)["observations"][0]
    assert row["maker_amounts_reconciled"]
    assert row["cash_micro"]==c and row["shares_micro"]==q
    assert row["hypotheses"]["7/100"]["fee_micro"]["FLOOR_PER_MAKER_FILL"]==87180


@pytest.mark.parametrize("kind",["chain","reorg","future"])
def test_noncanonical_chain_or_block_is_not_admitted(kind):
    block,rows=fixture();original=transport(block,rows)
    def changed(url,request):
        response=audit.strict_json(original(url,request))
        if kind=="chain" and request["method"]=="eth_chainId": response["result"]="0x1"
        if kind=="reorg" and request["id"]==5: response["result"]={**block,"hash":hx(9999)}
        if kind=="future" and request["id"]==2: response["result"]={**block,"number":"0x1233"}
        return audit.canonical(response)
    receipt=audit.collect(block["hash"],changed)
    assert receipt["state"]=="SOURCE_OR_BINDING_ERROR" and receipt["proof_material"] is None
