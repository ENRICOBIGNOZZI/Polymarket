"""Bounded rational resource selection and durable ZERO-AUTHORITY holds.

This is a shadow planning component, not a broker, canonical ledger, or fill
engine. Scores are supplied causal conservative scenario scores, never inferred
from future fills. Unknown release times hold resources indefinitely. It must
not be used to sum independently simulated episodes into portfolio PnL.
"""
from collections import defaultdict
from fractions import Fraction
import json
import sqlite3

from v7_exact_arb_causal import ResourceLedger
from v7_exact_arb_native_evidence import SAFETY, boundary, canonical, integer
from v7_unified_exact_arb_graph import GraphError, frac, fstr, prove, sha, fee_per_share, round_fee
from v7_unified_exact_arb_graph_execution_shadow import order_limit


def require(ok, reason):
    if not ok: raise GraphError("resource_"+reason)


def rational(value):
    require(isinstance(value,(str,int)) and not isinstance(value,bool) and len(str(value))<=160,"rational")
    result=frac(value)
    require(result.numerator.bit_length()<=256 and result.denominator.bit_length()<=256,"rational_bounds")
    return result


def vector(raw):
    require(isinstance(raw,dict) and len(raw)<=131072,"vector_size")
    out={}
    for key,value in raw.items():
        require(isinstance(key,str) and 0<len(key)<=512 and all(ord(c)>=32 for c in key),"name")
        amount=rational(value);require(amount>=0,"negative_quantity")
        out[key]=amount
    return out


def normalized(raw):
    return {k:fstr(v) for k,v in sorted(raw.items())}


def rounding_partition_upper_bound(raw_total, increment, mode):
    """Bound SUM of fees under any fragmentation of the declared fee model.

    VENUE_5DP rounds half-up to h and drops raw amounts below h. For x>=h,
    round(x/h)/(x/h) <= 4/3, tight at x=3h/2. Thus SUM rounded fees <=
    4/3 SUM raw fees, independently of fragment count. This proves the bound
    under the repository's model, NOT that the venue currently implements it.
    """
    total=rational(raw_total);require(total>=0,"fee_total")
    if mode=="EXACT": return total
    require(mode=="VENUE_5DP" and rational(increment)==Fraction(1,100000),"fee_rounding_unknown")
    return total*Fraction(4,3)


def native_request(candidate):
    """Pin worst-limit entry funding and depth claims using decision books only.

    Fees include a fragmentation-independent bound. Unwind funding is reserved
    separately, not deducted as if every complete fill necessarily unwinds.
    The score is a full-fill conditional bound, NOT unconditional expected PnL.
    """
    require(all(candidate.get(k) is v for k,v in SAFETY.items() if k!="execution_authority")
            and candidate.get("execution_authority")=="ZERO_AUTHORITY_RESEARCH_ONLY","paper_boundary")
    require(candidate.get("schema")=="polymarket_v7_native_exact_arb_execution_candidate_v1","candidate_schema")
    relation=candidate["relation"];prove(relation)
    require(relation.get("transformation") is None,"transformation_operational_terms_required")
    books=candidate["decision_books"]
    require(candidate.get("decision_books_sha256")==sha(books),"decision_books_digest")
    now=rational(candidate["timestamp_ms"])
    until=rational(relation["settlement_close_ms"])
    require(0<=now<until,"source_expired")
    require((now*1000000).denominator==1 and (until*1000000).denominator==1,"time_precision")
    direction=candidate["result"]["direction"]
    require(direction in {"BUY","SELL"},"direction")
    q=rational(candidate["result"]["quantity"]);guarantee=rational(relation["guaranteed_payout"])
    reserve=rational(relation["reserve_per_unit"])
    require(q>0 and guarantee>0 and reserve>=guarantee/2000,"reserve_or_quantity")
    required=defaultdict(Fraction);depth_capacities={};limits={};claims=[]
    entry_cash=entry_fees=unwind_fees=unwind_cash=Fraction(0)
    for leg in relation["legs"]:
        token=leg["token_id"];book=books[token]
        quantity=q*rational(leg["coefficient"])
        require(quantity>0 and (quantity*100).denominator==1 and quantity>=rational(leg["minimum_order"]),"order_quantity")
        limit=order_limit(leg,book,quantity,direction,now,100)
        rate=rational(leg["fee_rate"]);exponent=rational(leg["fee_exponent"])
        require(0<=rate<=1 and exponent.denominator==1 and 0<=exponent<=16,"fee_terms")
        require(rational(book["fee_rate"])==rate and rational(book["fee_exponent"])==exponent,"book_fee_mismatch")
        # Maximum fee curve over all prices satisfying the pinned order limit.
        peak=min(limit,Fraction(1,2)) if direction=="BUY" else max(limit,Fraction(1,2))
        raw_fee=quantity*rate*(peak*(1-peak))**int(exponent)
        raw_unwind_fee=quantity*rate*Fraction(1,4)**int(exponent)
        if rate:
            entry_fees+=rounding_partition_upper_bound(fstr(raw_fee),leg.get("fee_rounding_increment"),leg.get("fee_rounding_mode"))
            unwind_fees+=rounding_partition_upper_bound(fstr(raw_unwind_fee),leg.get("fee_rounding_increment"),leg.get("fee_rounding_mode"))
        entry_cash+=quantity*limit
        if direction=="SELL":
            required["inventory:"+token]+=quantity
            unwind_cash+=quantity  # no assumption that sale proceeds are reusable
        remaining=quantity;side="asks" if direction=="BUY" else "bids"
        for price,size in book[side]:
            price,size=rational(price),rational(size)
            take=min(remaining,size)
            if take<=0: break
            # Deliberately not keyed by state_version: a heartbeat or unrelated
            # book update must not grant a fresh copy of the same displayed size.
            key="depth:"+sha([candidate["observer_session_id"],token,side,fstr(price)])
            required[key]+=take;depth_capacities[key]=size
            remaining-=take
        require(remaining==0,"decision_depth")
        limits[token]=fstr(limit)
        claims.append([token,fstr(quantity),leg["payout_vector"]])
    reserve_cash=q*reserve
    required["PUSD"]=(entry_cash if direction=="BUY" else unwind_cash)+entry_fees+unwind_fees+reserve_cash
    score=(q*guarantee-entry_cash if direction=="BUY" else entry_cash-q*guarantee)-entry_fees-reserve_cash
    return {**SAFETY,"schema":"polymarket_v7_exact_arb_resource_request_v1","model_sha":candidate["model_sha"],
        "request_id":candidate["opportunity_id"],"portfolio_id":sha([direction,sorted(claims),fstr(q*guarantee)]),
        "decision_available_ns":int(now*1000000),"valid_until_ns":int(until*1000000),
        "resources":normalized(required),"selection_score":fstr(score),
        "score_kind":"WORST_LIMIT_FULL_FILL_NET_PNL_NOT_EXPECTED_VALUE",
        "depth_capacities":normalized(depth_capacities),"order_limits":limits,
        "funding":{"entry_fee_upper_bound":fstr(entry_fees),"unwind_fee_upper_bound":fstr(unwind_fees),
            "unwind_cash_bound":fstr(unwind_cash),"reserve_buffer":fstr(reserve_cash)},
        "source_candidate_sha256":sha(candidate),"release_time_ns":None,
        "resource_admission_verified":False,"venue_execution_verified":False}


def select(requests, capacities, now_ns, model, maximum_nodes=100000):
    """Deterministic bounded branch-and-bound; exact rational objective/bounds."""
    require(isinstance(requests,list) and len(requests)<=64,"batch_capacity")
    require(type(maximum_nodes) is int and 1<=maximum_nodes<=1000000,"search_budget")
    require(type(now_ns) is int and 0<=now_ns<2**63,"decision_time")
    capacities=vector(capacities)
    accepted=[];rejected={};portfolios={};ids=set();raw_paths=len(requests)
    require(all(isinstance(row,dict) for row in requests),"request_object")
    for row in sorted(requests,key=lambda row:str(row.get("request_id"))):
        boundary(row)
        require(row.get("schema")=="polymarket_v7_exact_arb_resource_request_v1" and row.get("model_sha")==model,"request_identity")
        identifier=row.get("request_id");portfolio=row.get("portfolio_id")
        require(isinstance(identifier,str) and 0<len(identifier)<=256 and identifier not in ids,"request_id")
        require(isinstance(portfolio,str) and len(portfolio)==64,"portfolio_id")
        ids.add(identifier)
        available=integer(row,"decision_available_ns");until=integer(row,"valid_until_ns",1)
        require(available<until and available<=now_ns,"future_or_invalid_request")
        require(row.get("score_kind")=="WORST_LIMIT_FULL_FILL_NET_PNL_NOT_EXPECTED_VALUE","score_semantics")
        score=rational(row["selection_score"]);resources=vector(row["resources"])
        require(bool(resources) and any(resources.values()),"empty_request")
        signature=canonical([normalized(resources),fstr(score)])
        if portfolio in portfolios:
            require(portfolios[portfolio][0]==signature,"conflicting_duplicate_portfolio")
            rejected[identifier]="DUPLICATE_PORTFOLIO";continue
        portfolios[portfolio]=(signature,identifier)
        if now_ns>=until: rejected[identifier]="EXPIRED";continue
        if score<=0: rejected[identifier]="NONPOSITIVE_CONDITIONAL_BOUND";continue
        if any(k not in capacities for k in resources): rejected[identifier]="UNKNOWN_RESOURCE_CAPACITY";continue
        if any(v>capacities[k] for k,v in resources.items()): rejected[identifier]="INSUFFICIENT_RESOURCE_CAPACITY";continue
        accepted.append((identifier,score,resources))
    accepted.sort(key=lambda row:(-row[1],row[0]))
    suffix=[Fraction(0)]*(len(accepted)+1)
    for i in range(len(accepted)-1,-1,-1): suffix[i]=suffix[i+1]+accepted[i][1]
    used=defaultdict(Fraction);chosen=[];best=Fraction(0);best_ids=();visited=0;cut_bound=Fraction(0);work=0

    def search(index,score):
        nonlocal best,best_ids,visited,cut_bound,work
        upper=score+suffix[index]
        if upper<best: return
        if visited>=maximum_nodes:
            cut_bound=max(cut_bound,upper);return
        visited+=1
        ordered=tuple(sorted(chosen))
        if score>best or (score==best and ordered<best_ids): best,best_ids=score,ordered
        if index==len(accepted): return
        identifier,value,requirements=accepted[index]
        if work+len(requirements)>2000000:
            cut_bound=max(cut_bound,upper);return
        work+=len(requirements)
        if all(used[k]+v<=capacities[k] for k,v in requirements.items()):
            for k,v in requirements.items(): used[k]+=v
            chosen.append(identifier);search(index+1,score+value);chosen.pop()
            for k,v in requirements.items(): used[k]-=v
        search(index+1,score)

    search(0,Fraction(0))
    requirements=defaultdict(Fraction)
    for identifier,_,resources in accepted:
        if identifier in best_ids:
            for k,v in resources.items(): requirements[k]+=v
        else: rejected[identifier]="RESOURCE_CONFLICT_OR_SEARCH_BOUND"
    upper=max(best,cut_bound)
    return {**SAFETY,"schema":"polymarket_v7_exact_arb_resource_selection_v1","model_sha":model,
        "decision_ns":now_ns,"selected_ids":list(best_ids),"rejected":dict(sorted(rejected.items())),
        "reserved_vector":normalized(requirements),"conditional_score":fstr(best),"score_upper_bound":fstr(upper),
        "optimal_value_proven":upper==best,"search_nodes":visited,"maximum_search_nodes":maximum_nodes,
        "constraint_entries_examined":work,"maximum_constraint_entries":2000000,
        "raw_paths":raw_paths,"canonical_portfolios":len(portfolios),"duplicate_paths_removed":raw_paths-len(portfolios),
        "expected_pnl":None,"resource_admission_verified":False,"venue_execution_verified":False}


class ShadowResourceJournal:
    """Transactional research holds; never releases on guessed settlement/TTL.

    Uses the existing quantity ResourceLedger for admission. SQLite serializes
    writers and persists reservations/decision IDs across research restarts.
    Release/settlement requires a separate causal evidence adapter; until then
    every reservation remains encumbered, including after source lease expiry.
    """
    RELEASE_POLICY="NO_RELEASE_WITHOUT_CAUSAL_SETTLEMENT_EVIDENCE"
    JOURNAL_MODE="NO_RELEASE_BASELINE"
    def __init__(self, path, model):
        self.model=model;self.db=sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS resource_meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS resource_holds(id TEXT PRIMARY KEY,body TEXT,request_hash TEXT);
            CREATE TABLE IF NOT EXISTS resource_decisions(id TEXT PRIMARY KEY,digest TEXT,body TEXT);
            CREATE TABLE IF NOT EXISTS resource_admission_inputs(id TEXT PRIMARY KEY,body TEXT);
        """)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            existing=self.db.execute("SELECT value FROM resource_meta WHERE key='model'").fetchone()
            require(existing is None or existing[0]==model,"journal_model")
            mode=self.db.execute("SELECT value FROM resource_meta WHERE key='journal_mode'").fetchone()
            if mode is None:
                # Legacy populated journals cannot silently gain release rights.
                capital=self.db.execute("SELECT value FROM resource_meta WHERE key='capital_world'").fetchone()
                populated=self.db.execute("SELECT 1 FROM resource_holds LIMIT 1").fetchone()
                inferred="CAUSAL_MODEL_WORLD" if capital else "NO_RELEASE_BASELINE"
                require(not (capital or populated) or self.JOURNAL_MODE==inferred,"journal_mode")
            else:
                require(mode[0]==self.JOURNAL_MODE,"journal_mode")
            self.db.execute("INSERT OR IGNORE INTO resource_meta VALUES('journal_mode',?)",(self.JOURNAL_MODE,))
            self.db.execute("INSERT OR IGNORE INTO resource_meta VALUES('model',?)",(model,))
            self.db.commit()
        except Exception:
            self.db.rollback();self.db.close();raise

    def close(self): self.db.close()

    def _capacities(self, capacities):
        return capacities

    def admit(self, batch_id, requests, snapshot, now_ns, maximum_nodes=100000):
        require(isinstance(requests,list) and len(requests)<=64,"batch_capacity")
        boundary(snapshot)
        require(snapshot.get("schema")=="polymarket_v7_exact_arb_shadow_capacity_v1"
                and snapshot.get("model_sha")==self.model,"snapshot_identity")
        require(snapshot.get("capacity_basis")=="TOTAL_BEFORE_RESEARCH_HOLDS_NOT_EXECUTION_AUTHORITY","capacity_basis")
        require(integer(snapshot,"available_ns")<=now_ns<integer(snapshot,"expires_ns",1),"snapshot_lease")
        capacities=vector(snapshot.get("capacities"))
        require(isinstance(batch_id,str) and 0<len(batch_id)<=256,"batch_id")
        digest=sha([requests,snapshot,now_ns,maximum_nodes])
        try:
            self.db.execute("BEGIN IMMEDIATE")
            previous=self.db.execute("SELECT digest,body FROM resource_decisions WHERE id=?",(batch_id,)).fetchone()
            if previous:
                require(previous[0]==digest,"conflicting_batch_replay")
                self.db.commit();return json.loads(previous[1])
            last=self.db.execute("SELECT value FROM resource_meta WHERE key='time'").fetchone()
            require(last is None or now_ns>=int(last[0]),"clock_reversal")
            capacities=self._capacities(capacities)
            ledger=ResourceLedger()
            for identifier,body in self.db.execute("SELECT id,body FROM resource_holds"):
                ledger.reservations[identifier]={"release_ms":None,"resources":normalized(vector(json.loads(body)))}
            used=ledger.used()
            require(all(k in capacities and v<=capacities[k] for k,v in used.items() if not k.startswith("depth:")),
                    "encumbered_financial_capacity_missing_or_shrunk")
            available={k:fstr(max(Fraction(0),v-used[k])) for k,v in capacities.items()}
            seen=dict(self.db.execute("SELECT id,request_hash FROM resource_holds"))
            for row in requests:
                boundary(row)
                if row.get("request_id") in seen:
                    require(seen[row["request_id"]]==sha(row),"conflicting_reserved_request")
            eligible=[row for row in requests if row.get("request_id") not in seen]
            receipt=select(eligible,available,now_ns,self.model,maximum_nodes)
            for row in requests:
                if row.get("request_id") in seen: receipt["rejected"][row["request_id"]]="ALREADY_RESERVED"
            for row in eligible:
                if row["request_id"] not in receipt["selected_ids"]: continue
                require(ledger.reserve(row["request_id"],row["resources"],normalized(capacities),now_ns,None),"atomic_admission")
                self.db.execute("INSERT INTO resource_holds VALUES(?,?,?)",(row["request_id"],canonical(row["resources"]),sha(row)))
                self.db.execute("INSERT INTO resource_admission_inputs VALUES(?,?)",(row["request_id"],canonical(row)))
            receipt.update(batch_id=batch_id,input_sha256=digest,holds_after=normalized(ledger.used()),
                           release_policy=self.RELEASE_POLICY)
            self.db.execute("INSERT INTO resource_decisions VALUES(?,?,?)",(batch_id,digest,canonical(receipt)))
            self.db.execute("INSERT OR REPLACE INTO resource_meta VALUES('time',?)",(str(now_ns),))
            self.db.commit();return receipt
        except Exception:
            self.db.rollback();raise


class ShadowCapitalJournal(ShadowResourceJournal):
    """Opt-in per-world model cash/inventory lifecycle, NEVER venue settlement.

    The original no-release journal remains an independent planning baseline.
    Closed modeled fills alter this world's balances only. Reserve is retained
    as an encumbrance, not misreported as a venue cash payment. Acquired tokens
    are inventory, never automatically redeemed for their terminal guarantee.
    """
    POLICY="CAUSAL_MODEL_FILL_LIFECYCLE_V1"
    JOURNAL_MODE="CAUSAL_MODEL_WORLD"
    RELEASE_POLICY="MODELED_ACK_CLOSED_ORDERS_ONLY_NO_SETTLEMENT_OR_VENUE_RELEASE_ATTESTATION"

    def __init__(self,path,model,world_id,initial_resources=None):
        require(isinstance(world_id,str) and 0<len(world_id)<=256,"capital_world_identity")
        super().__init__(path,model)
        self.world_id=world_id
        self.db.execute("CREATE TABLE IF NOT EXISTS capital_results(id TEXT PRIMARY KEY,digest TEXT,body TEXT)")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            identity=canonical([self.POLICY,world_id])
            old=self.db.execute("SELECT value FROM resource_meta WHERE key='capital_world'").fetchone()
            require(old is None or old[0]==identity,"capital_world_identity")
            self.db.execute("INSERT OR IGNORE INTO resource_meta VALUES('capital_world',?)",(identity,))
            if initial_resources is not None:
                initial=vector(initial_resources)
                require(all(not key.startswith("depth:") for key in initial),"capital_initial_resource_kind")
                self._capacities(initial)
            self.db.commit()
        except Exception:
            self.db.rollback();self.db.close();raise

    def _meta_vector(self,key):
        row=self.db.execute("SELECT value FROM resource_meta WHERE key=?",(key,)).fetchone()
        return {} if row is None else {k:rational(v) for k,v in json.loads(row[0]).items()}

    def _capacities(self,capacities):
        original={k:v for k,v in capacities.items() if not k.startswith("depth:")}
        old=self.db.execute("SELECT value FROM resource_meta WHERE key='initial_financial_capacity'").fetchone()
        wire=canonical(normalized(original))
        require(old is None or old[0]==wire,"capital_initial_capacity_changed")
        self.db.execute("INSERT OR IGNORE INTO resource_meta VALUES('initial_financial_capacity',?)",(wire,))
        result=dict(capacities)
        for key,delta in self._meta_vector("financial_deltas").items(): result[key]=result.get(key,Fraction(0))+delta
        require(all(value>=0 for value in result.values()),"capital_negative_balance")
        return result

    @staticmethod
    def _fill_amounts(row,leg,expected,now_ns,after_ns):
        require(row.get("token_id")==leg["token_id"],"capital_fill_token")
        requested=rational(row["requested_size"]);filled=rational(row["filled_size"])
        require(requested==expected and 0<=filled<=requested
                and rational(row["remaining_quantity"])==requested-filled,"capital_fill_quantity")
        times=[rational(row[k])*1000000 for k in ("submission_timestamp_ms","wire_arrival_timestamp_ms",
            "match_timestamp_ms","result_timestamp_ms")]
        require(all(t.denominator==1 for t in times) and after_ns<=times[0]<=times[1]<=times[2]<=times[3]<now_ns,
                "capital_future_or_unacknowledged_fill")
        fills=row.get("fills")
        require(isinstance(fills,list) and len(fills)<=1024,"capital_fill_depth")
        qty=cash=fees=Fraction(0)
        for raw in fills:
            require(isinstance(raw,list) and len(raw)==2,"capital_fill_shape")
            price,size=map(rational,raw)
            require(0<price<1 and size>0,"capital_fill_terms")
            qty+=size;cash+=price*size
            fees+=round_fee(size*fee_per_share(price,rational(leg["fee_rate"]),int(rational(leg["fee_exponent"]))),
                frac(leg.get("fee_rounding_increment")) if leg.get("fee_rounding_increment") else None,
                leg.get("fee_rounding_mode") or "EXACT")
        require(qty==filled and cash==rational(row["notional"]) and fees==rational(row["fee"]),"capital_fill_accounting")
        return filled,cash,fees

    def complete(self,candidate,result,now_ns):
        require(type(now_ns) is int and 0<now_ns<2**63,"capital_time")
        require(all(result.get(k) is v for k,v in SAFETY.items() if k!="execution_authority")
                and (result.get("execution_authority",False) is False or result.get("execution_authority")=="ZERO_AUTHORITY_RESEARCH_ONLY")
                and result.get("venue_execution_verified") is False,"capital_result_authority")
        identifier=candidate["opportunity_id"]
        require(result.get("opportunity_id")==identifier and result.get("model_sha")==self.model
                and result.get("shared_world_id")==self.world_id,"capital_result_identity")
        digest=sha([candidate,result,now_ns])
        try:
            self.db.execute("BEGIN IMMEDIATE")
            previous=self.db.execute("SELECT digest,body FROM capital_results WHERE id=?",(identifier,)).fetchone()
            if previous:
                require(previous[0]==digest,"capital_conflicting_result")
                self.db.commit();return json.loads(previous[1])
            last=self.db.execute("SELECT value FROM resource_meta WHERE key='time'").fetchone()
            require(last is None or now_ns>=int(last[0]),"clock_reversal")
            admitted=self.db.execute("SELECT body FROM resource_admission_inputs WHERE id=?",(identifier,)).fetchone()
            require(admitted is not None,"capital_unreserved_result")
            request=json.loads(admitted[0])
            require(request.get("source_candidate_sha256")==sha(candidate),"capital_candidate_binding")
            require(sha(native_request(candidate))==sha(request),"capital_request_projection")
            hold=vector(json.loads(self.db.execute("SELECT body FROM resource_holds WHERE id=?",(identifier,)).fetchone()[0]))
            state=result.get("state");delta=defaultdict(Fraction);reserve=Fraction(0);flat_pnl=None
            require(state in {"NO_LEGS_FILLED","PARTIAL_UNWOUND","ALL_LEGS_FILLED","CENSORED",
                              "EXPOSURE_REMAINS","TRANSFORMATION_SUCCESS_UNOBSERVED"},"capital_result_state")
            closed=state in {"NO_LEGS_FILLED","PARTIAL_UNWOUND","ALL_LEGS_FILLED"}
            if closed:
                relation=candidate["relation"];legs={leg["token_id"]:leg for leg in relation["legs"]}
                require(relation.get("transformation") is None,"capital_transformation_success_unverified")
                rows=result.get("legs")
                require(isinstance(rows,list) and len(rows)==len(legs)
                        and {row.get("token_id") for row in rows}==set(legs),"capital_incomplete_legs")
                direction=candidate["result"]["direction"];sign=1 if direction=="BUY" else -1
                require(direction in {"BUY","SELL"},"capital_direction")
                q=rational(candidate["result"]["quantity"]);sizes={};fees=Fraction(0)
                for row in rows:
                    token=row["token_id"];leg=legs[token]
                    size,cash,fee=self._fill_amounts(row,leg,q*rational(leg["coefficient"]),now_ns,request["decision_available_ns"])
                    sizes[token]=size;delta["inventory:"+token]+=sign*size
                    delta["PUSD"]-=sign*cash+fee;fees+=fee
                require(fees<=rational(request["funding"]["entry_fee_upper_bound"]),"capital_entry_fee_bound")
                if state=="NO_LEGS_FILLED":
                    require(not any(sizes.values()) and not result.get("unwind_legs"),"capital_false_zero_fill")
                    flat_pnl=Fraction(0)
                elif state=="ALL_LEGS_FILLED":
                    require(all(sizes[t]==q*rational(legs[t]["coefficient"]) for t in legs)
                            and not result.get("unwind_legs"),"capital_false_complete_fill")
                    reserve=rational(request["funding"]["reserve_buffer"])
                    # Tokens remain in inventory. There is no payout credit.
                else:
                    require(any(sizes.values()) and any(sizes[t]<q*rational(legs[t]["coefficient"]) for t in legs),
                            "capital_false_partial_fill")
                    unwind=result.get("unwind_legs")
                    require(isinstance(unwind,list) and len(unwind)<=len(legs),"capital_unwind_legs")
                    seen=set();unwind_fees=Fraction(0)
                    entry_known=max(rational(row["result_timestamp_ms"])*1000000 for row in rows)
                    for row in unwind:
                        token=row.get("token_id")
                        require(token in legs and token not in seen and sizes[token]>0,"capital_unwind_token")
                        seen.add(token)
                        size,cash,fee=self._fill_amounts(row,legs[token],sizes[token],now_ns,entry_known)
                        require(size==sizes[token],"capital_unwind_incomplete")
                        delta["inventory:"+token]-=sign*size;delta["PUSD"]+=sign*cash-fee;unwind_fees+=fee
                    require(not any(v for k,v in delta.items() if k.startswith("inventory:")),"capital_residual_inventory")
                    require(unwind_fees<=rational(request["funding"]["unwind_fee_upper_bound"]),"capital_unwind_fee_bound")
                    reserve=rational(request["funding"]["reserve_buffer"])
                    flat_pnl=delta["PUSD"]-reserve
                require(not result.get("unhedged_exposure"),"capital_claimed_residual")
                require(rational(result.get("reserve_drag"))==reserve,"capital_reserve_accounting")
                if flat_pnl is not None:
                    require(rational(result.get("realized_counterfactual_pnl"))==flat_pnl,"capital_pnl_accounting")
                require(max(Fraction(0),-delta["PUSD"])+reserve<=hold.get("PUSD",0),"capital_cash_exceeds_hold")
                for k,v in delta.items():
                    if k.startswith("inventory:") and v<0: require(-v<=hold.get(k,0),"capital_inventory_exceeds_hold")
                cumulative=self._meta_vector("financial_deltas")
                for k,v in delta.items(): cumulative[k]=cumulative.get(k,Fraction(0))+v
                self.db.execute("INSERT OR REPLACE INTO resource_meta VALUES('financial_deltas',?)",(canonical(normalized(cumulative)),))
                replacement={"PUSD":fstr(reserve)} if reserve else {}
                self.db.execute("UPDATE resource_holds SET body=? WHERE id=?",(canonical(replacement),identifier))
            receipt={**SAFETY,"schema":"polymarket_v7_shadow_capital_transition_v1","model_sha":self.model,
                "world_id":self.world_id,"request_id":identifier,"input_sha256":digest,"available_ns":now_ns,
                "execution_state":state,"policy":self.POLICY,"modeled_orders_closed":closed,
                "decision_available_ns":request["decision_available_ns"],
                "reserved_pusd":fstr(hold.get("PUSD",0)),
                "reservation_duration_ns":now_ns-request["decision_available_ns"] if closed else None,
                "reservation_pusd_seconds":fstr(hold.get("PUSD",0)*Fraction(now_ns-request["decision_available_ns"],1000000000)) if closed else None,
                "capital_time_scope":"RESERVATION_TO_RESULT_ONLY_NOT_TOTAL_INVENTORY_OR_SETTLEMENT_LOCK",
                "modeled_financial_delta":normalized(delta),"retained_reserve":fstr(reserve) if closed else None,
                "modeled_flat_net_pnl":fstr(flat_pnl) if flat_pnl is not None else None,
                "venue_execution_verified":False,"resource_release_verified":False,
                "settlement_release_verified":False,"scope":"HYPOTHETICAL_WORLD_NOT_VENUE_BALANCE_OR_REALIZED_PROFIT"}
            self.db.execute("INSERT INTO capital_results VALUES(?,?,?)",(identifier,digest,canonical(receipt)))
            self.db.execute("INSERT OR REPLACE INTO resource_meta VALUES('time',?)",(str(now_ns),))
            self.db.commit();return receipt
        except Exception:
            self.db.rollback();raise

    def capital_receipt(self):
        # Read all balances, holds and outcomes from one SQLite snapshot even
        # when a second research writer commits a fill concurrently.
        self.db.execute("BEGIN")
        try:
            receipt=self._capital_receipt()
            self.db.commit();return receipt
        except Exception:
            self.db.rollback();raise

    def _capital_receipt(self):
        initial=self._meta_vector("initial_financial_capacity");delta=self._meta_vector("financial_deltas")
        totals=defaultdict(Fraction,initial)
        for k,v in delta.items(): totals[k]+=v
        held=defaultdict(Fraction)
        for (body,) in self.db.execute("SELECT body FROM resource_holds"):
            for k,v in vector(json.loads(body)).items():
                if not k.startswith("depth:"): held[k]+=v
        closed_pnl=Fraction(0);uncertain=0;closed=0
        for (body,) in self.db.execute("SELECT body FROM capital_results"):
            row=json.loads(body);closed+=row["modeled_orders_closed"];uncertain+=not row["modeled_orders_closed"]
            if row["modeled_flat_net_pnl"] is not None: closed_pnl+=rational(row["modeled_flat_net_pnl"])
        pending=self.db.execute("SELECT COUNT(*) FROM resource_holds h LEFT JOIN capital_results r "
                                "ON h.id=r.id WHERE r.id IS NULL").fetchone()[0]
        return {**SAFETY,"world_id":self.world_id,"policy":self.POLICY,"initial_resources":normalized(initial),
            "modeled_balances":normalized(totals),"encumbered_resources":normalized(held),
            "available_financial_resources":normalized({k:v-held[k] for k,v in totals.items()}),
            "modeled_flat_net_pnl":fstr(closed_pnl),"closed_order_groups":closed,"unresolved_order_groups":uncertain+pending,
            "pending_order_groups":pending,"censored_order_groups":uncertain,
            "portfolio_net_pnl":None,"resource_release_verified":False,"settlement_release_verified":False,
            "venue_execution_verified":False,"capital_capacity_curve_verified":False,
            "balance_scope":"ACCOUNTED_CLOSED_MODEL_FILLS_ONLY_UNRESOLVED_FLOWS_ENCUMBERED_NOT_POSTED"}
