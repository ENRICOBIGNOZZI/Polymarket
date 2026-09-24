#!/usr/bin/env python3
"""Read-only, block-pinned NegRisk evidence. Never an executable attestation.

Two public RPC transcripts corroborate membership independently of Gamma. This
does NOT prove exactly-one terminal outcome, freeze future membership, verify
deployed bytecode against audited source, or grant transformation authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.request

SCHEMA = "polymarket_v7_negrisk_block_attestation_v1"
MAX_COLLECTION_NS = 360_000_000_000
SAFETY = dict(paper_only=True, authenticated_execution=False, real_order_submission=False,
              real_capital_at_risk=False, automatic_promotion=False, execution_authority=False)
RPC_URLS = ("https://polygon-bor-rpc.publicnode.com", "https://1rpc.io/matic")
WRAPPER = "0xada2005600dec949baf300f4c6120000bdb6eaab"
ADAPTER = "0xd91e80cf2e7be2e162c6513ced06f1dd0da35296"
CTF = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
PUSD = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
USDCE = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
SELECTORS = {
    "NEG_RISK_ADAPTER()": "f6f88a8d", "WRAPPED_COLLATERAL()": "2d277260",
    "CONDITIONAL_TOKENS()": "165d1f36", "COLLATERAL_TOKEN()": "f5f1f1a7", "USDCE()": "195187e1",
    "ctf()": "22a9339f", "col()": "a78695b0", "wcol()": "7e3b74c3",
    "getMarketData(bytes32)": "30f4f4bb", "getQuestionCount(bytes32)": "b7f75d2c",
    "getFeeBips(bytes32)": "2582cb5e", "getConditionId(bytes32)": "04329c03",
    "getPositionId(bytes32,bool)": "752b5ba5", "getOutcomeSlotCount(bytes32)": "d42dc0c2",
    "payoutDenominator(bytes32)": "dd34de67", "payoutNumerators(bytes32,uint256)": "0504c814",
    "paused(address)": "2e48152c",
}
SOURCES = {
    "contracts": "https://docs.polymarket.com/resources/contracts",
    "negative_risk": "https://docs.polymarket.com/concepts/negative-risk",
    "adapter_source": "https://github.com/Polymarket/neg-risk-ctf-adapter/tree/f78b35b0863b4308a431ca307d06f49b2ea65e78",
    "wrapper_source": "https://github.com/Polymarket/ctf-exchange-v2/tree/ccc0596074f4dfd62c944fbca4de252893b82b4b",
}


def require(value, reason):
    if not value: raise ValueError(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    def bad_constant(_): raise ValueError("nonfinite_json")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_constant)


def hex_value(value, size, nonzero=True):
    require(isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{"+str(size*2)+"}", value), "invalid_hex")
    require(not nonzero or int(value,16)>0, "zero_identity")
    return value.lower()


def quantity(value):
    require(isinstance(value,str) and re.fullmatch(r"0x(?:0|[1-9a-f][0-9a-f]*)",value), "rpc_quantity")
    require(len(value)<=66,"rpc_quantity_bound")
    return int(value,16)


def word(value):
    return int(hex_value(value,32,False),16)


def address_word(value):
    number=word(value)
    require(0<number<2**160,"abi_address")
    return f"0x{number:040x}"


def call(address, signature, block_hash, *args):
    require(signature in SELECTORS,"unsupported_view")
    encoded=[]
    for arg in args:
        number=int(arg,16) if isinstance(arg,str) else arg
        require(type(number) is int and 0<=number<2**256,"abi_argument")
        encoded.append(f"{number:064x}")
    return ["eth_call", [{"to":hex_value(address,20),"data":"0x"+SELECTORS[signature]+"".join(encoded)},
                         {"blockHash":hex_value(block_hash,32),"requireCanonical":True}]]


def validate_query(query):
    require(isinstance(query,list) and len(query)==2,"rpc_query")
    method,params=query
    require(isinstance(params,list),"rpc_params")
    if method=="eth_chainId": require(params==[],"rpc_chain_args");return
    if method=="eth_getBlockByNumber":
        require(len(params)==2 and params[1] is False,"rpc_block_args")
        if params[0]!="finalized": quantity(params[0])
        return
    require(method in {"eth_getCode","eth_call"} and len(params)==2,"readonly_rpc_method")
    pin=params[1]
    require(isinstance(pin,dict) and set(pin)=={"blockHash","requireCanonical"}
            and pin["requireCanonical"] is True,"unbound_rpc_block")
    hex_value(pin["blockHash"],32)
    if method=="eth_getCode": hex_value(params[0],20);return
    target=params[0]
    require(isinstance(target,dict) and set(target)=={"to","data"},"readonly_call_fields")
    hex_value(target["to"],20)
    data=target["data"]
    require(isinstance(data,str) and re.fullmatch(r"0x[0-9a-f]+",data),"call_data")
    matches=[sig for sig,selector in SELECTORS.items() if data[2:10]==selector]
    require(len(matches)==1,"readonly_call_selector")
    args=matches[0].split("(")[1][:-1]
    count=0 if not args else len(args.split(","))
    require(len(data)==10+64*count,"call_data_length")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): raise ValueError("source_redirect")


def fetch(url, payload=None):
    require(url in RPC_URLS or re.fullmatch(r"https://gamma-api.polymarket.com/events/[1-9][0-9]*",url),"source_allowlist")
    request=urllib.request.Request(url, data=None if payload is None else canonical(payload).encode(),
        headers={"User-Agent":"Polymarket-ExactArb-Paper-Research/1.0","Content-Type":"application/json"})
    with urllib.request.build_opener(NoRedirect).open(request,timeout=8) as response:
        raw=response.read(4*1024*1024+1)
    require(len(raw)<=4*1024*1024,"source_response_bound")
    return raw.decode("utf-8")


class Rpc:
    def __init__(self,url,transport=fetch):
        require(url in RPC_URLS,"rpc_allowlist")
        self.url,self.transport,self.records=url,transport,[]
        self.sequence=0;self.bytes=0;self.started=time.monotonic()
        self.next_request=self.started

    def read(self,query): return self.many([query])[0]

    def many(self,queries):
        require(isinstance(queries,list) and len(queries)<=2048,"rpc_query_bound")
        for query in queries: validate_query(query)
        values=[]
        for start in range(0,len(queries),16):
            if self.transport is fetch:
                # Public providers count individual calls within batches. Pace
                # requests; never switch identity or bypass provider limits.
                time.sleep(max(0,self.next_request-time.monotonic()))
                self.next_request=time.monotonic()+1
            require(time.monotonic()-self.started<=MAX_COLLECTION_NS/1e9,"rpc_time_budget")
            part=queries[start:start+16];request=[]
            for method,params in part:
                self.sequence+=1;require(self.sequence<=2048,"rpc_request_budget")
                request.append(dict(jsonrpc="2.0",id=self.sequence,method=method,params=params))
            raw=self.transport(self.url,request)
            self.bytes+=len(raw.encode());require(self.bytes<=16*1024*1024,"rpc_bytes_budget")
            response=strict_json(raw)
            require(isinstance(response,list) and len(response)==len(request),"rpc_batch_shape")
            by_id={}
            for row in response:
                require(isinstance(row,dict) and row.get("jsonrpc")=="2.0" and type(row.get("id")) is int
                        and row["id"] not in by_id and set(row)=={"jsonrpc","id","result"},"rpc_error_or_id")
                by_id[row["id"]]=row["result"]
            require(set(by_id)=={r["id"] for r in request},"rpc_response_ids")
            self.records.append(dict(request=request,response_raw=raw))
            values.extend(by_id[r["id"]] for r in request)
        return values


def inspect(event,rpc,block):
    require(isinstance(event,dict) and (event.get("negRisk") is True or event.get("enableNegRisk") is True),"not_negrisk")
    eid=event.get("id");require(isinstance(eid,str) and re.fullmatch(r"[1-9][0-9]*",eid),"event_identity")
    group=hex_value(event.get("negRiskMarketID"),32)
    require(int(group,16)%256==0,"group_index_bits")
    rows=event.get("markets")
    require(isinstance(rows,list) and 1<=len(rows)<=255 and all(isinstance(r,dict) for r in rows),"event_members")
    block_hash=hex_value(block["hash"],32);number=block["number"]
    quantity(number);stamp=quantity(block["timestamp"])
    require(quantity(rpc.read(["eth_chainId",[]]))==137,"chain_id")
    before=rpc.read(["eth_getBlockByNumber",[number,False]])
    require(isinstance(before,dict) and all(before.get(k)==block[k] for k in ("hash","number","timestamp")),"block_not_canonical")
    signatures=["NEG_RISK_ADAPTER()","WRAPPED_COLLATERAL()","CONDITIONAL_TOKENS()","COLLATERAL_TOKEN()","USDCE()"]
    binding=dict(zip(signatures,map(address_word,rpc.many([call(WRAPPER,s,block_hash) for s in signatures]))))
    require(binding["NEG_RISK_ADAPTER()"]==ADAPTER and binding["CONDITIONAL_TOKENS()"]==CTF
            and binding["COLLATERAL_TOKEN()"]==PUSD and binding["USDCE()"]==USDCE,"wrapper_contract_binding")
    inner=rpc.many([call(ADAPTER,s,block_hash) for s in ("ctf()","col()","wcol()")])
    require(list(map(address_word,inner))==[CTF,USDCE,binding["WRAPPED_COLLATERAL()"]],"adapter_contract_binding")
    codes=rpc.many([["eth_getCode",[a,{"blockHash":block_hash,"requireCanonical":True}]] for a in (WRAPPER,ADAPTER,CTF)])
    code_hashes={}
    for address,code in zip((WRAPPER,ADAPTER,CTF),codes):
        require(isinstance(code,str) and re.fullmatch(r"0x(?:[0-9a-f]{2})+",code) and len(code)<=200002,"empty_or_invalid_code")
        code_hashes[address]=hashlib.sha256(bytes.fromhex(code[2:])).hexdigest()
    state,count,fee=rpc.many([call(ADAPTER,s,block_hash,group) for s in
                            ("getMarketData(bytes32)","getQuestionCount(bytes32)","getFeeBips(bytes32)")])
    packed=bytes.fromhex(hex_value(state,32)[2:]);count=word(count);fee=word(fee)
    require(2<=count<=255 and count==packed[0] and fee==int.from_bytes(packed[3:5],"big") and fee<=10000
            and packed[1] in (0,1) and packed[5:12]==bytes(7) and int.from_bytes(packed[12:],"big")>0
            and (packed[2]<count if packed[1] else packed[2]==0),"market_data")
    questions=[f"0x{int(group,16)+i:064x}" for i in range(count)]
    calls=[]
    for q in questions:
        calls += [call(ADAPTER,"getConditionId(bytes32)",block_hash,q),
                  call(ADAPTER,"getPositionId(bytes32,bool)",block_hash,q,1),
                  call(ADAPTER,"getPositionId(bytes32,bool)",block_hash,q,0)]
    values=rpc.many(calls);members=[]
    for i,q in enumerate(questions):
        condition=hex_value(values[3*i],32);yes,no=word(values[3*i+1]),word(values[3*i+2])
        require(yes>0 and no>0 and yes!=no,"token_identity")
        members.append(dict(index=i,question_id=q,condition_id=condition,yes_token=str(yes),no_token=str(no)))
    require(len({m["condition_id"] for m in members})==count
            and len({m[k] for m in members for k in ("yes_token","no_token")})==2*count,"chain_member_collision")
    calls=[]
    for m in members:
        c=m["condition_id"]
        calls += [call(CTF,"getOutcomeSlotCount(bytes32)",block_hash,c),call(CTF,"payoutDenominator(bytes32)",block_hash,c),
                  call(CTF,"payoutNumerators(bytes32,uint256)",block_hash,c,0),call(CTF,"payoutNumerators(bytes32,uint256)",block_hash,c,1)]
    payouts=list(map(word,rpc.many(calls)))
    for i,m in enumerate(members):
        slots,den,yes,no=payouts[4*i:4*i+4]
        require(slots==2 and den in (0,1) and yes+no==den,"condition_payout_state")
        m.update(payout_denominator=den,payout_numerators=[yes,no])
    winners=[m["index"] for m in members if m["payout_numerators"][0]==1]
    require(winners==([packed[2]] if packed[1] else []),"determined_payout_conflict")
    metadata={}
    for row in rows:
        mid=row.get("id");require(isinstance(mid,str) and re.fullmatch(r"[1-9][0-9]*",mid),"market_id")
        condition=hex_value(row.get("conditionId"),32)
        require(condition not in metadata and mid not in {r["id"] for r in metadata.values()},"metadata_member_collision")
        metadata[condition]=row
    missing=[]
    for m in members:
        row=metadata.pop(m["condition_id"],None)
        if row is None: missing.append(m["index"]);m["market_id"]=None;continue
        require(row.get("negRisk") is True,"member_negrisk_flag")
        if row.get("negRiskMarketID") is not None: require(hex_value(row["negRiskMarketID"],32)==group,"member_group_conflict")
        outcomes=strict_json(row["outcomes"]) if isinstance(row.get("outcomes"),str) else row.get("outcomes")
        tokens=strict_json(row["clobTokenIds"]) if isinstance(row.get("clobTokenIds"),str) else row.get("clobTokenIds")
        require(isinstance(outcomes,list) and isinstance(tokens,list) and len(outcomes)==len(tokens)==2
                and all(isinstance(o,str) for o in outcomes),"metadata_outcomes")
        mapping=dict(zip([o.upper() for o in outcomes],tokens))
        require(mapping=={"YES":m["yes_token"],"NO":m["no_token"]},"metadata_token_binding")
        # Gamma questionID can be the UMA request, not the adapter question ID.
        m.update(market_id=row["id"],observed_gamma_question_id=row.get("questionID"),
                 observed_other=row.get("negRiskOther") if type(row.get("negRiskOther")) is bool else None)
    require(not metadata,"metadata_nonmember_condition")
    pause=word(rpc.read(call(WRAPPER,"paused(address)",block_hash,USDCE)))
    require(pause in (0,1),"pause_state")
    after=rpc.read(["eth_getBlockByNumber",[number,False]])
    require(isinstance(after,dict) and all(after.get(k)==block[k] for k in ("hash","number","timestamp")),"block_reorg")
    return dict(event_id=eid,group_id=group,block=dict(number=number,hash=block_hash,timestamp=stamp),
        contract_bindings=binding,code_sha256=code_hashes,market_data=state,question_count=count,fee_bips=fee,
        members=members,missing_gamma_indices=missing,metadata_covers_chain_members=not missing,
        determined=bool(packed[1]),result_index=packed[2] if packed[1] else None,wrapper_paused=bool(pause),
        augmented=event.get("negRiskAugmented") if type(event.get("negRiskAugmented")) is bool else None,
        resolved_single_winner_observed=all(m["payout_denominator"]==1 for m in members) and len(winners)==1)


def projection(observed):
    reasons=["DEPLOYED_BYTECODE_SOURCE_BINDING_UNVERIFIED","FUTURE_MEMBERSHIP_NOT_FROZEN",
             "EXACTLY_ONE_TERMINAL_OUTCOME_NOT_PROVEN","CONVERSION_OPERATIONAL_READINESS_UNVERIFIED"]
    if observed["augmented"] is not False: reasons.append("AUGMENTED_OR_UNKNOWN_PLACEHOLDER_SEMANTICS")
    if observed["missing_gamma_indices"]: reasons.append("GAMMA_OMITS_CHAIN_MEMBERS")
    if observed["wrapper_paused"]: reasons.append("WRAPPER_PAUSED")
    return dict(**observed,verification="UNVERIFIED_CANDIDATE",source_trust="CORROBORATED_PUBLIC_RPC_STATE_OBSERVATION",
        membership_at_block_observed=True,membership_stable=False,complete_set_verified=False,conversion_verified=False,
        state_proof_verified=False,runtime_code_source_verified=False,valid_until=None,reasons=reasons,
        market_ids=[m["market_id"] for m in observed["members"]],conditions=[m["condition_id"] for m in observed["members"]],
        membership_hash=sha(observed["members"]))


def collect(event_raw,transport=fetch):
    started=time.time_ns();mono=time.monotonic_ns()
    receipt=dict(schema=SCHEMA,**SAFETY,verification_timestamp=started,valid_until=None,
        verification_source=list(RPC_URLS),sources=SOURCES,event_raw=event_raw,complete_set_verified=False,
        conversion_verified=False,verification="UNVERIFIED_CANDIDATE",rpc_evidence=[],proof_material=None)
    clients=[]
    try:
        require(isinstance(event_raw,str) and len(event_raw.encode())<=4*1024*1024,"event_size")
        event=strict_json(event_raw)
        clients=[Rpc(url,transport) for url in RPC_URLS]
        block=clients[0].read(["eth_getBlockByNumber",["finalized",False]])
        require(isinstance(block,dict) and all(k in block for k in ("number","hash","timestamp")),"finalized_block")
        require(0<=started//1000000000-quantity(block["timestamp"])<=900,"block_age_or_clock")
        observations=[inspect(event,rpc,block) for rpc in clients]
        require(observations[0]==observations[1],"rpc_state_disagreement")
        receipt["proof_material"]=projection(observations[0])
        receipt["state"]="CORROBORATED_BLOCK_SNAPSHOT_NOT_EXECUTABLE"
    except (ValueError,KeyError,TypeError,OSError,TimeoutError) as error:
        receipt.update(state="SOURCE_OR_BINDING_ERROR",error=str(error)[:256])
    receipt["rpc_evidence"]=[dict(url=c.url,records=c.records) for c in clients]
    receipt["completed_at_ns"]=time.time_ns();receipt["duration_ns"]=time.monotonic_ns()-mono
    if receipt["completed_at_ns"]<started or abs(receipt["completed_at_ns"]-started-receipt["duration_ns"])>1000000000:
        receipt.update(state="SOURCE_OR_BINDING_ERROR",error="collection_clock",proof_material=None)
    if receipt["completed_at_ns"]-started>MAX_COLLECTION_NS:
        receipt.update(state="SOURCE_OR_BINDING_ERROR",error="collection_time_budget",proof_material=None)
    receipt["proof_hash"]=sha(receipt)
    return receipt


def validate_envelope(receipt):
    require(isinstance(receipt,dict) and receipt.get("schema")==SCHEMA
            and all(receipt.get(k) is v for k,v in SAFETY.items()),"attestation_boundary")
    body={k:v for k,v in receipt.items() if k!="proof_hash"}
    require(sha(body)==receipt.get("proof_hash"),"attestation_hash")
    require(receipt.get("complete_set_verified") is False and receipt.get("conversion_verified") is False
            and receipt.get("valid_until") is None and receipt.get("sources")==SOURCES
            and receipt.get("verification")=="UNVERIFIED_CANDIDATE","attestation_authority")
    start,end=receipt.get("verification_timestamp"),receipt.get("completed_at_ns")
    require(type(start) is int and type(end) is int and 0<=end-start<=MAX_COLLECTION_NS,"attestation_time")
    duration=receipt.get("duration_ns")
    require(type(duration) is int and duration>=0 and abs(end-start-duration)<=1000000000,"attestation_clock")
    require(receipt.get("verification_source")==list(RPC_URLS) and len(receipt["rpc_evidence"])==2,"attestation_sources")
    require(isinstance(receipt.get("event_raw"),str) and len(receipt["event_raw"].encode())<=4*1024*1024,"event_size")


def source_observation(receipt,source_index):
    """Recover ONLY a fully recorded single-source observation after a peer fails.

    This never upgrades the failed two-source receipt. No latest-source fallback,
    no network, and no substitution of one provider for two independent reads.
    """
    validate_envelope(receipt)
    require(type(source_index) is int and source_index in (0,1),"source_index")
    event=strict_json(receipt["event_raw"])
    block=None;observed=None
    for index in range(source_index+1):
        source=receipt["rpc_evidence"][index]
        require(source["url"]==RPC_URLS[index] and isinstance(source["records"],list),"attestation_sources")
        iterator=iter(source["records"])
        def replay(url,request):
            row=next(iterator,None);require(row is not None and row["request"]==request,"rpc_transcript_query")
            return row["response_raw"]
        rpc=Rpc(source["url"],replay)
        if index==0:
            block=rpc.read(["eth_getBlockByNumber",["finalized",False]])
            require(0<=receipt["verification_timestamp"]//1000000000-quantity(block["timestamp"])<=900,"block_age_or_clock")
        if index==source_index:
            observed=inspect(event,rpc,block)
            require(next(iterator,None) is None,"rpc_transcript_tail")
    return {**projection(observed),"source_trust":"SINGLE_PUBLIC_RPC_STATE_OBSERVATION",
            "source_url":RPC_URLS[source_index],"cross_source_agreement_verified":False}


def validate_receipt(receipt):
    validate_envelope(receipt)
    require(receipt.get("state")=="CORROBORATED_BLOCK_SNAPSHOT_NOT_EXECUTABLE","attestation_source_failed")
    observations=[]
    for index in (0,1):
        observed=source_observation(receipt,index)
        observed.pop("source_url");observed.pop("cross_source_agreement_verified")
        observed["source_trust"]="CORROBORATED_PUBLIC_RPC_STATE_OBSERVATION"
        observations.append(observed)
    require(observations[0]==observations[1] and observations[0]==receipt["proof_material"],"attestation_projection")
    return receipt["proof_material"]


def conversion_model(receipt,index_set,amount):
    """Resource-vector preview only, no signing, eth_call conversion or orders."""
    observed=validate_receipt(receipt);n=observed["question_count"]
    require(type(index_set) is int and 0<index_set<2**n,"conversion_index_set")
    require(type(amount) is int and 0<amount<2**256,"conversion_amount")
    require(amount*observed["fee_bips"]<2**256
            and amount*n<2**256,"conversion_uint256_overflow")
    fee=amount*observed["fee_bips"]//10000;out=amount-fee
    selected=[m for m in observed["members"] if index_set>>m["index"]&1]
    return dict(**SAFETY,verification="UNVERIFIED_CANDIDATE",conversion_verified=False,
        source_attestation=receipt["proof_hash"],source_block=observed["block"],
        inputs={m["no_token"]:amount for m in selected},
        minimum_modeled_yes_outputs={m["yes_token"]:out for m in observed["members"] if not index_set>>m["index"]&1},
        modeled_collateral_output=(len(selected)-1)*out,fee_per_input=fee,
        gas_cost=None,latency_ns=None,operational_ready=False,
        scope="PINNED_SOURCE_FORMULA_NOT_DEPLOYED_CODE_OR_EXECUTION_PROOF")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    choice=parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--event-id");choice.add_argument("--inspect-receipt",type=Path)
    parser.add_argument("--source-index",type=int,choices=(0,1),default=0)
    parser.add_argument("--output-directory",type=Path)
    args=parser.parse_args()
    if args.inspect_receipt:
        require(args.inspect_receipt.stat().st_size<=40*1024*1024,"receipt_size")
        receipt=strict_json(args.inspect_receipt.read_text())
        observed=source_observation(receipt,args.source_index)
        print(canonical({k:observed[k] for k in ("source_trust","source_url","cross_source_agreement_verified",
            "event_id","group_id","block","question_count","missing_gamma_indices","augmented","fee_bips",
            "membership_hash","resolved_single_winner_observed","complete_set_verified","conversion_verified","reasons")}))
        return 0
    require(args.output_directory is not None,"output_directory_required")
    require(re.fullmatch(r"[1-9][0-9]*",args.event_id),"event_identity")
    raw=fetch("https://gamma-api.polymarket.com/events/"+args.event_id)
    receipt=collect(raw)
    args.output_directory.mkdir(parents=True,exist_ok=True)
    path=args.output_directory/(receipt["proof_hash"]+".json")
    encoded=canonical(receipt)+"\n"
    if path.exists(): require(path.read_text()==encoded,"archive_conflict")
    else:
        with path.open("x") as stream: stream.write(encoded)
    summary={k:receipt.get(k) for k in ("state","error","proof_hash","complete_set_verified","conversion_verified")}
    if receipt["proof_material"]:
        material=validate_receipt(receipt)
        summary.update({k:material[k] for k in ("event_id","question_count","missing_gamma_indices","augmented","fee_bips","reasons")})
    print(canonical(summary))
    return 0 if receipt["proof_material"] else 2


if __name__=="__main__": raise SystemExit(main())
