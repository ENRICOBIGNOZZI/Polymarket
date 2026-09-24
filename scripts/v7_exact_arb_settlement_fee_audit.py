"""Public finalized-block fee observations; never executable fee attestations.

Compare explicit rate/rounding hypotheses with actual OrderFilled amounts.
Historical fee schedules, deployed source binding and RPC completeness remain
unverified. Fitting a rate to a fill is NOT a verified rate or a fee-free market.
"""
import argparse
from collections import Counter, defaultdict
from fractions import Fraction as F
from pathlib import Path
import time

from v7_exact_arb_negrisk_attestation import (
    SAFETY, RPC_URLS, canonical, fetch, hex_value, quantity, require, sha, strict_json)

SCHEMA = "polymarket_v7_public_settlement_fee_audit_v1"
EXCHANGES = ("0xe111180000d2663c0091e4f400237545b87b996b", "0xe2222d279d744050d28e00520010520000310f59")
SIGNATURES = {
    "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)":
        "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee",
    "OrdersMatched(bytes32,address,uint8,uint256,uint256,uint256)":
        "0x174b3811690657c217184f89418266767c87e4805d09680c39fc9c031c0cab7c",
}
FILLED, MATCHED = SIGNATURES.values()
RATES = ("0", "1/50", "3/100", "1/25", "1/20", "7/100")
SOURCE = "https://github.com/Polymarket/ctf-exchange-v2/tree/ccc0596074f4dfd62c944fbca4de252893b82b4b"


def words(value, count):
    raw = hex_value(value, 32*count, False)[2:]
    return [int(raw[i:i+64],16) for i in range(0,len(raw),64)]


def account(topic):
    value = hex_value(topic,32)
    require(int(value,16)<2**160,"event_address_padding")
    return "0x"+value[-40:]


def fee_micro(shares_micro, cash_micro, rate, mode):
    """Amount hypotheses, with exact execution ratio (not rounded display tick)."""
    p = F(cash_micro,shares_micro)
    units = F(shares_micro,10)*F(rate)*p*(1-p)  # 1e-5 collateral units
    if units<1: return 0
    if mode=="HALF_UP": units+=F(1,2)
    else: require(mode=="FLOOR","rounding_hypothesis")
    return 10*(units.numerator//units.denominator)


def analyze(block, logs):
    block_hash = hex_value(block["hash"],32)
    number = quantity(block["number"])
    require(isinstance(logs,list) and len(logs)<=8192,"log_budget")
    groups=defaultdict(list);observations=[];last=-1;fills=0
    for row in logs:
        require(isinstance(row,dict) and row.get("removed") is False,"removed_or_invalid_log")
        address=hex_value(row["address"],20)
        require(address in EXCHANGES and hex_value(row["blockHash"],32)==block_hash
                and quantity(row["blockNumber"])==number,"log_block_or_exchange")
        index=quantity(row["logIndex"])
        require(index>last,"duplicate_or_unordered_log");last=index
        tx=hex_value(row["transactionHash"],32)
        quantity(row["transactionIndex"])
        topics=row["topics"]
        require(isinstance(topics,list) and 1<=len(topics)<=4,"event_topics")
        for topic in topics: hex_value(topic,32,False)
        key=(tx,address)
        if topics[0]==FILLED:
            require(len(topics)==4,"filled_topics")
            side,token,making,taking,fee,builder,metadata=words(row["data"],7)
            require(side in (0,1) and token>0 and making>0 and taking>0,"fill_amounts")
            cash,shares=(making,taking) if side==0 else (taking,making)
            require(0<cash<shares,"fill_price")
            groups[key].append(dict(order=topics[1],maker=account(topics[2]),taker=account(topics[3]),
                side=side,token=str(token),making=making,taking=taking,fee=fee,log_index=index))
            fills+=1
        elif topics[0]==MATCHED:
            require(len(topics)==3,"matched_topics")
            side,token,making,taking=words(row["data"],4)
            entries=groups.pop(key,[])
            require(len(entries)>=2,"matched_without_complete_fill_group")
            taker=entries[-1];makers=entries[:-1]
            require(taker["order"]==topics[1] and taker["maker"]==account(topics[2])
                    and taker["taker"]==address and (taker["side"],taker["token"],taker["making"],taker["taking"])
                    ==(side,str(token),making,taking),"taker_matched_binding")
            require(all(m["taker"]==taker["maker"] and m["taker"]!=address for m in makers),"maker_taker_binding")
            cash,shares=(making,taking) if side==0 else (taking,making)
            parts=[]
            for maker in makers:
                if maker["side"]!=side:
                    require(maker["token"]==str(token),"complementary_token_binding")
                    q,c=(maker["making"],maker["taking"]) if side==0 else (maker["taking"],maker["making"])
                else:
                    require(maker["token"]!=str(token),"split_merge_token_binding")
                    q,c=(maker["taking"],maker["taking"]-maker["making"]) if side==0 else (maker["making"],maker["making"]-maker["taking"])
                require(0<c<q,"maker_implied_price")
                parts.append((q,c))
            reconciled=sum(q for q,c in parts)==shares and sum(c for q,c in parts)==cash
            levels=defaultdict(lambda:[0,0])
            for q,c in parts:
                levels[F(c,q)][0]+=q;levels[F(c,q)][1]+=c
            hypotheses={}
            for rate in RATES:
                estimates={mode+"_AGGREGATE_VWAP":fee_micro(shares,cash,rate,mode) for mode in ("FLOOR","HALF_UP")}
                if reconciled:
                    for mode in ("FLOOR","HALF_UP"):
                        estimates[mode+"_PER_MAKER_FILL"]=sum(fee_micro(q,c,rate,mode) for q,c in parts)
                        estimates[mode+"_PER_PRICE_LEVEL"]=sum(fee_micro(q,c,rate,mode) for q,c in levels.values())
                hypotheses[rate]={"fee_micro":estimates,"matching_models":[name for name,fee in estimates.items() if fee==taker["fee"]]}
            observations.append(dict(transaction_hash=tx,exchange=address,taker_log_index=taker["log_index"],
                token_id=str(token),side="BUY" if side==0 else "SELL",shares_micro=shares,cash_micro=cash,
                observed_fee_micro=taker["fee"],fee_is_multiple_of_10_micro=taker["fee"]%10==0,
                maker_count=len(makers),maker_amounts_reconciled=reconciled,
                historical_rate_verified=False,rounding_mode_verified=False,hypotheses=hypotheses))
    require(not any(groups.values()),"unmatched_fill_tail")
    fit=Counter()
    for row in observations:
        if row["observed_fee_micro"]:
            for rate,models in row["hypotheses"].items():
                for name in models["matching_models"]: fit[rate+":"+name]+=1
    return dict(scope="HISTORICAL_SINGLE_PUBLIC_RPC_SETTLEMENT_NOT_CAUSAL_EXECUTION_EVIDENCE",
        block={k:block[k] for k in ("hash","number","timestamp")},logs=len(logs),order_filled_events=fills,
        taker_matches=len(observations),positive_fee_matches=sum(r["observed_fee_micro"]>0 for r in observations),
        rate_hypotheses=list(RATES),positive_fee_hypothesis_matches=dict(sorted(fit.items())),
        observations=observations,historical_rate_verified=False,venue_execution_verified=False,
        rpc_completeness_verified=False,runtime_code_source_verified=False,
        limitation="Fit counts overlap. Never select a fee rule/rate from these counts; no historical fee schedule or generalization to future matches.")


def observe(block_hash, transport):
    block_hash=hex_value(block_hash,32)
    sequence=0
    def rpc(method,params):
        nonlocal sequence
        sequence+=1
        request={"jsonrpc":"2.0","id":sequence,"method":method,"params":params}
        row=strict_json(transport(RPC_URLS[0],request))
        require(isinstance(row,dict) and set(row)=={"jsonrpc","id","result"}
                and row["jsonrpc"]=="2.0" and type(row["id"]) is int and row["id"]==sequence,"rpc_response")
        return row["result"]
    require(quantity(rpc("eth_chainId",[]))==137,"chain_id")
    finalized=rpc("eth_getBlockByNumber",["finalized",False])
    block=rpc("eth_getBlockByHash",[block_hash,False])
    require(isinstance(block,dict) and hex_value(block["hash"],32)==block_hash
            and quantity(block["number"])<=quantity(finalized["number"]),"not_finalized")
    quantity(block["timestamp"])
    logs=rpc("eth_getLogs",[{"blockHash":block_hash,"address":list(EXCHANGES)}])
    after=rpc("eth_getBlockByNumber",[block["number"],False])
    require(all(after[k]==block[k] for k in ("hash","number","timestamp")),"block_reorg")
    return analyze(block,logs)


def collect(block_hash, transport=fetch):
    start=time.monotonic();records=[];size=0
    def recorded(url,request):
        nonlocal size
        require(time.monotonic()-start<60 and len(records)<5,"collection_budget")
        raw=transport(url,request);size+=len(raw.encode())
        require(size<=8*1024*1024,"source_size")
        records.append(dict(request=request,response_raw=raw))
        return raw
    result=dict(schema=SCHEMA,**SAFETY,source=RPC_URLS[0],contract_source=SOURCE,
        block_hash=block_hash,verification_timestamp_ns=time.time_ns(),valid_until=None,
        venue_execution_verified=False,records=records,proof_material=None)
    try:
        result["proof_material"]=observe(block_hash,recorded);result["state"]="HISTORICAL_OBSERVATION_ONLY"
    except (ValueError,KeyError,TypeError,OSError,TimeoutError) as error:
        result.update(state="SOURCE_OR_BINDING_ERROR",error=str(error)[:256])
    result["proof_hash"]=sha(result)
    return result


def validate(receipt):
    require(isinstance(receipt,dict) and receipt.get("schema")==SCHEMA
            and all(receipt.get(k) is v for k,v in SAFETY.items()),"receipt_boundary")
    require(receipt.get("proof_hash")==sha({k:v for k,v in receipt.items() if k!="proof_hash"}),"receipt_hash")
    require(receipt.get("state")=="HISTORICAL_OBSERVATION_ONLY" and receipt.get("source")==RPC_URLS[0]
            and receipt.get("contract_source")==SOURCE and receipt.get("valid_until") is None
            and receipt.get("venue_execution_verified") is False,"receipt_authority")
    require(isinstance(receipt.get("records"),list) and len(receipt["records"])==5,"transcript_size")
    require(sum(len(r["response_raw"].encode()) for r in receipt["records"])<=8*1024*1024,"source_size")
    records=iter(receipt["records"])
    def replay(url,request):
        row=next(records,None)
        require(row is not None and row["request"]==request,"transcript_query")
        return row["response_raw"]
    result=observe(receipt["block_hash"],replay)
    require(result==receipt["proof_material"],"receipt_projection")
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block-hash",required=True)
    parser.add_argument("--output-directory",required=True,type=Path)
    args=parser.parse_args();receipt=collect(args.block_hash)
    if receipt["proof_material"]: validate(receipt)
    args.output_directory.mkdir(parents=True,exist_ok=True)
    path=args.output_directory/(receipt["proof_hash"]+".json");wire=canonical(receipt)+"\n"
    if path.exists(): require(path.read_text()==wire,"archive_conflict")
    else:
        with path.open("x") as stream: stream.write(wire)
    material=receipt["proof_material"] or {}
    print(canonical(dict(path=str(path),state=receipt["state"],error=receipt.get("error"),
        taker_matches=material.get("taker_matches"),positive_fee_matches=material.get("positive_fee_matches"),
        venue_execution_verified=False)))
    return 0 if receipt["proof_material"] else 2


if __name__=="__main__": raise SystemExit(main())
