from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import v7_exact_arb_negrisk_attestation as nr
from v7_unified_exact_arb_graph import compile_graph, SAFETY


def hx(number): return f"0x{number:064x}"


class Chain:
    def __init__(self, count=3, **options):
        self.n=count;self.options=options;self.requests=[];self.number_reads={}
        self.block={"number":"0x1234","hash":hx(888),"timestamp":hex(int(time.time())-10)}
        self.group=hx(999*256);self.wcol="0x"+"99"*20
        self.event={"id":"123","negRisk":True,"negRiskMarketID":self.group,"negRiskAugmented":False,
            "markets":[{"id":str(100+i),"conditionId":hx(1000+i),"negRisk":True,
                        "negRiskMarketID":self.group,"questionID":hx(999999+i),
                        "outcomes":["Yes","No"],"clobTokenIds":[str(2000+2*i),str(2001+2*i)]}
                       for i in range(count)]}

    def response(self,url,request):
        self.requests.append((url,deepcopy(request)))
        method,params=request["method"],request["params"]
        if method=="eth_chainId": return "0x1" if self.options.get("wrong_chain") else "0x89"
        if method=="eth_getBlockByNumber":
            if params[0]!="finalized": self.number_reads[url]=self.number_reads.get(url,0)+1
            return {**self.block,"hash":hx(889)} if self.options.get("reorg") and self.number_reads.get(url,0)>=2 else self.block
        if method=="eth_getCode": return "0x" if self.options.get("empty_code") else "0x60016000"
        assert method=="eth_call"
        data=params[0]["data"];sig={v:k for k,v in nr.SELECTORS.items()}[data[2:10]]
        args=[int(data[i:i+64],16) for i in range(10,len(data),64)]
        addresses={"NEG_RISK_ADAPTER()":nr.ADAPTER,"WRAPPED_COLLATERAL()":self.wcol,
            "CONDITIONAL_TOKENS()":nr.CTF,"COLLATERAL_TOKEN()":nr.PUSD,"USDCE()":nr.USDCE,
            "ctf()":nr.CTF,"col()":nr.USDCE,"wcol()":self.wcol}
        if sig in addresses:
            value=addresses[sig]
            if self.options.get("wrong_wrapper") and sig=="NEG_RISK_ADAPTER()": value=nr.WRAPPER
            return hx(int(value,16))
        count=self.n+(1 if self.options.get("disagree") and url==nr.RPC_URLS[1] else 0)
        fee=self.options.get("fee",37)
        resolved=self.options.get("resolved",False)
        winner=resolved and not self.options.get("all_false")
        if sig=="getMarketData(bytes32)": return "0x"+(bytes([count,int(winner),0])+fee.to_bytes(2,"big")+bytes(7)+bytes.fromhex("55"*20)).hex()
        if sig=="getQuestionCount(bytes32)": return hx(count+int(self.options.get("count_conflict",False)))
        if sig=="getFeeBips(bytes32)": return hx(fee)
        if sig=="getConditionId(bytes32)": return hx(1000+args[0]-int(self.group,16))
        if sig=="getPositionId(bytes32,bool)": return hx(2000+2*(args[0]-int(self.group,16))+(0 if args[1] else 1))
        if sig=="getOutcomeSlotCount(bytes32)": return hx(2)
        if sig=="payoutDenominator(bytes32)": return hx(int(resolved))
        if sig=="payoutNumerators(bytes32,uint256)":
            yes=winner and args[0]==1000
            return hx(int(resolved and (yes if args[1]==0 else not yes)))
        if sig=="paused(address)": return hx(int(self.options.get("paused",False)))
        raise AssertionError(sig)

    def transport(self,url,requests):
        if self.options.get("timeout"): raise TimeoutError("provider_timeout")
        response=[dict(jsonrpc="2.0",id=r["id"],result=self.response(url,r)) for r in requests]
        if self.options.get("wrong_id"): response[0]["id"]+=9999
        if self.options.get("rpc_error"): response[0]={"jsonrpc":"2.0","id":requests[0]["id"],"error":{"code":-32000}}
        # JSON-RPC responses can legitimately arrive out of request order.
        return nr.canonical(list(reversed(response)))

    def collect(self): return nr.collect(nr.canonical(self.event),self.transport)


def rehash(receipt):
    receipt["proof_hash"]=nr.sha({k:v for k,v in receipt.items() if k!="proof_hash"})
    return receipt


def test_two_rpc_pinned_membership_does_not_prove_exhaustiveness_or_execution():
    chain=Chain();receipt=chain.collect();observed=nr.validate_receipt(receipt)
    assert observed["question_count"]==3 and observed["metadata_covers_chain_members"]
    assert observed["missing_gamma_indices"]==[] and observed["fee_bips"]==37
    assert observed["members"][0]["question_id"]!=observed["members"][0]["observed_gamma_question_id"]
    assert observed["market_ids"]==["100","101","102"]
    for key in ("membership_stable","complete_set_verified","conversion_verified","state_proof_verified","runtime_code_source_verified"):
        assert observed[key] is False
    assert receipt["valid_until"] is None
    for _,request in chain.requests:
        assert request["method"] in {"eth_chainId","eth_getBlockByNumber","eth_getCode","eth_call"}
        if request["method"] in {"eth_getCode","eth_call"}:
            assert request["params"][1]=={"blockHash":chain.block["hash"],"requireCanonical":True}


def test_hidden_members_are_retained_not_dropped_to_make_a_partition():
    chain=Chain(5);chain.event["markets"]=chain.event["markets"][:2]
    observed=nr.validate_receipt(chain.collect())
    assert observed["question_count"]==5 and len(observed["conditions"])==5
    assert observed["missing_gamma_indices"]==[2,3,4] and not observed["metadata_covers_chain_members"]
    assert observed["market_ids"]==["100","101",None,None,None]
    assert "GAMMA_OMITS_CHAIN_MEMBERS" in observed["reasons"]


@pytest.mark.parametrize("augmentation",[True,None,"false",0])
def test_unknown_or_augmented_semantics_never_auto_enable(augmentation):
    chain=Chain();chain.event["negRiskAugmented"]=augmentation
    observed=nr.validate_receipt(chain.collect())
    assert observed["augmented"] is (True if augmentation is True else None)
    assert "AUGMENTED_OR_UNKNOWN_PLACEHOLDER_SEMANTICS" in observed["reasons"]


@pytest.mark.parametrize("all_false",[True,False])
def test_resolved_payouts_are_observed_without_assuming_one_winner(all_false):
    observed=nr.validate_receipt(Chain(resolved=True,all_false=all_false).collect())
    assert observed["resolved_single_winner_observed"] is (not all_false)
    assert sum(m["payout_numerators"][0] for m in observed["members"])==int(not all_false)
    assert not observed["complete_set_verified"]


@pytest.mark.parametrize("option,reason",[
    ("wrong_chain","chain_id"),("reorg","block_reorg"),("empty_code","empty_or_invalid_code"),
    ("wrong_wrapper","wrapper_contract_binding"),("count_conflict","market_data"),
    ("disagree","rpc_state_disagreement"),("timeout","provider_timeout"),
    ("wrong_id","rpc_response_ids"),("rpc_error","rpc_error_or_id")])
def test_source_defects_are_failed_receipts_not_partial_success(option,reason):
    receipt=Chain(**{option:True}).collect()
    assert receipt["state"]=="SOURCE_OR_BINDING_ERROR" and receipt["error"]==reason
    assert receipt["proof_material"] is None and receipt["complete_set_verified"] is False
    with pytest.raises(ValueError,match="attestation_source_failed"): nr.validate_receipt(receipt)


@pytest.mark.parametrize("mutation,reason",[
    (lambda e:e["markets"].append(deepcopy(e["markets"][0])),"metadata_member_collision"),
    (lambda e:e["markets"][0].update(clobTokenIds=["2001","2000"]),"metadata_token_binding"),
    (lambda e:e["markets"][0].update(conditionId=hx(123456)),"metadata_nonmember_condition"),
    (lambda e:e["markets"][0].update(negRiskMarketID=hx(888*256)),"member_group_conflict"),
    (lambda e:e.update(negRiskMarketID=hx(999*256+1)),"group_index_bits"),
])
def test_metadata_must_bind_to_every_observed_claim(mutation,reason):
    chain=Chain();mutation(chain.event);receipt=chain.collect()
    assert receipt["error"]==reason and receipt["proof_material"] is None


@pytest.mark.parametrize("mutation",[
    lambda r:r.update(complete_set_verified=True),
    lambda r:r["proof_material"].update(complete_set_verified=True),
    lambda r:r["proof_material"]["members"][0].update(yes_token="123"),
    lambda r:r["rpc_evidence"][1].update(url=nr.RPC_URLS[0]),
    lambda r:r["rpc_evidence"][0]["records"][0]["request"][0].update(method="eth_sendRawTransaction"),
    lambda r:r["rpc_evidence"][1]["records"].pop(),
    lambda r:r.update(completed_at_ns=r["verification_timestamp"]-1),
])
def test_rehashed_claims_cannot_replace_raw_transcript_validation(mutation):
    receipt=Chain().collect();mutation(receipt);rehash(receipt)
    with pytest.raises(ValueError): nr.validate_receipt(receipt)


def test_conversion_fee_and_resource_vector_are_explicitly_non_executable():
    receipt=Chain(fee=37).collect();quote=nr.conversion_model(receipt,0b101,1000001)
    assert quote["inputs"]=={"2001":1000001,"2005":1000001}
    assert quote["minimum_modeled_yes_outputs"]=={"2002":996301}
    assert quote["modeled_collateral_output"]==996301 and quote["fee_per_input"]==3700
    assert quote["gas_cost"] is None and quote["latency_ns"] is None
    assert not quote["conversion_verified"] and not quote["operational_ready"]
    assert nr.conversion_model(receipt,0b111,1000001)["minimum_modeled_yes_outputs"]=={}
    for mask,amount in ((0,1),(8,1),(True,1),(1,True),(1,0),(1,2**256-1)):
        with pytest.raises(ValueError): nr.conversion_model(receipt,mask,amount)


@pytest.mark.parametrize("query",[
    ["eth_sendRawTransaction",["0x00"]], ["personal_sign",[]],
    ["eth_call",[{"to":nr.ADAPTER,"data":"0x095ea7b3"},"latest"]],
    ["eth_call",[{"to":nr.ADAPTER,"data":"0x"+nr.SELECTORS["ctf()"],"from":nr.PUSD},{"blockHash":hx(1),"requireCanonical":True}]],
    ["eth_getCode",[nr.CTF,"latest"]], ["eth_getBlockByNumber",["pending",False]],
])
def test_transport_cannot_submit_orders_or_query_unpinned_contract_state(query):
    chain=Chain();rpc=nr.Rpc(nr.RPC_URLS[0],chain.transport)
    with pytest.raises(ValueError): rpc.read(query)
    assert not chain.requests


def test_graph_import_retains_observation_without_enabling_an_equality():
    receipt=Chain().collect();model="a"*40
    registry={**SAFETY,"schema":"polymarket_v7_exact_arb_relation_registry_v1","relations":[]}
    graph=compile_graph([registry],{**SAFETY,"model_sha":model,"markets":[]},model,[receipt])
    assert graph["relations"]==[]
    assert graph["component_provenance"][0]["scope"]=="HISTORICAL_BLOCK_OBSERVATION_ONLY"
    assert graph["unverified_candidates"][0]["reason"]=="independent_terminal_exhaustiveness_unproven"
    receipt["proof_material"]["complete_set_verified"]=True;rehash(receipt)
    graph=compile_graph([registry],{**SAFETY,"model_sha":model,"markets":[]},model,[receipt])
    assert not graph["relations"] and graph["unverified_candidates"][0]["reason"]=="invalid_negrisk_block_attestation"


def test_complete_single_provider_trace_is_diagnostic_when_peer_fails():
    chain=Chain()
    def transport(url,request):
        if url==nr.RPC_URLS[1]: raise OSError("provider_quota")
        return chain.transport(url,request)
    receipt=nr.collect(nr.canonical(chain.event),transport)
    assert receipt["state"]=="SOURCE_OR_BINDING_ERROR"
    primary=nr.source_observation(receipt,0)
    assert primary["source_url"]==nr.RPC_URLS[0] and primary["question_count"]==3
    assert primary["source_trust"]=="SINGLE_PUBLIC_RPC_STATE_OBSERVATION"
    assert not primary["cross_source_agreement_verified"] and not primary["complete_set_verified"]
    with pytest.raises(ValueError,match="rpc_transcript_query"): nr.source_observation(receipt,1)
    with pytest.raises(ValueError,match="attestation_source_failed"): nr.validate_receipt(receipt)


@pytest.mark.parametrize("as_component",[False,True])
@pytest.mark.parametrize("family",["NEGRISK_COMPLETE_SET","EXPLICIT_EXACT"])
def test_explicit_registries_cannot_bypass_negrisk_semantic_gate(as_component,family):
    chain=Chain();model="a"*40
    markets=[dict(market_id=m["id"],condition_id=m["conditionId"],active=True,closed=False,
        neg_risk=True,clob_token_ids=m["clobTokenIds"],outcomes=m["outcomes"],fee_schedule={"rate":0})
        for m in chain.event["markets"]]
    relation=dict(id="claimed-partition",enabled=True,relation_family=family,
        states=["a","b","c"],guaranteed_payout=1,legs=[
            dict(selector={"market_id":m["market_id"]},outcome="YES",coefficient=1,
                 payout_vector=[int(i==j) for j in range(3)]) for i,m in enumerate(markets)])
    registry={**SAFETY,"schema":"polymarket_v7_exact_arb_relation_registry_v1","relations":[] if as_component else [relation]}
    components=[{**SAFETY,"model_sha":model,"schema":"research_component",
        "exact_relation_attestations":[{"verified":True,"relation":relation}]}] if as_component else []
    graph=compile_graph([registry],{**SAFETY,"model_sha":model,"markets":markets},model,components)
    assert len(graph["relations"])==1
    assert graph["relations"][0]["proof_type"]=="FINITE_STATE_EXACT_RATIONAL_SUM"
    assert not graph["relations"][0]["enabled"]
    assert graph["relations"][0]["verification"]=="UNVERIFIED_CANDIDATE"
    assert graph["metadata"]["actionable_relations"]==0


def test_same_condition_binary_remains_enabled_but_conversion_flag_is_insufficient():
    model="a"*40
    market=dict(market_id="1",condition_id=hx(1),active=True,closed=False,neg_risk=True,
        clob_token_ids=["100","101"],outcomes=["YES","NO"],fee_schedule={"rate":0})
    relation=dict(id="binary",enabled=True,relation_family="SAME_MARKET_BINARY_COMPLETE_SET",
        states=["yes","no"],guaranteed_payout=1,legs=[
            dict(selector={"market_id":"1"},outcome=outcome,coefficient=1,payout_vector=vector)
            for outcome,vector in (("YES",[1,0]),("NO",[0,1]))])
    registry={**SAFETY,"schema":"polymarket_v7_exact_arb_relation_registry_v1","relations":[relation]}
    universe={**SAFETY,"model_sha":model,"markets":[market]}
    assert compile_graph([registry],universe,model)["relations"][0]["enabled"]
    relation["transformation"]={"kind":"NEGRISK_CONVERSION","verification":"EXPLICIT_VERIFIED",
        "capacity":100,"latency_ms":1,"capital_lock_time_ms":1,"proof_hash":"b"*64}
    graph=compile_graph([registry],universe,model)
    assert not graph["relations"][0]["enabled"]
    assert graph["relations"][0]["semantic_rejection_reason"]=="independent_negrisk_exhaustiveness_or_conversion_proof_required"


def test_readonly_abi_selectors_match_existing_native_ethereum_keccak(tmp_path):
    compiler=shutil.which("c++");assert compiler
    binary=tmp_path/"selectors"
    subprocess.run([compiler,"-std=c++20","-O2","-I"+str(ROOT/"include"),
        str(ROOT/"src/v7_clob_eip712.cpp"),str(ROOT/"src/v7_keccak_fast.cpp"),
        str(ROOT/"tests/fixtures/v7_negrisk_readonly_selectors.cpp"),"-o",str(binary)],check=True,capture_output=True)
    result=subprocess.run([str(binary)],check=True,capture_output=True,text=True)
    from v7_exact_arb_settlement_fee_audit import SIGNATURES
    events=subprocess.run([str(binary),*SIGNATURES],check=True,capture_output=True,text=True)
    assert {k:"0x"+v for k,v in (line.split() for line in events.stdout.splitlines())}==SIGNATURES
    assert dict(line.split() for line in result.stdout.splitlines())==nr.SELECTORS


def test_nonfinite_and_duplicate_json_fail_closed():
    for raw in ('{"x":1,"x":2}','{"x":NaN}','{"x":Infinity}'):
        with pytest.raises(ValueError): nr.strict_json(raw)
