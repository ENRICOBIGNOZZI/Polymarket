"""Offline native evidence reduction; never an execution or profitability engine.

Read sealed tape segments in chronological order (current segment last). SQLite
keeps deduplication and exact descriptive quantiles off the feed thread and out
of unbounded Python lists. Re-running replaces reports; it never adds old counts.
Schema v1 lacks loss/continuity information and is deliberately not accepted.
"""
from __future__ import annotations

import argparse
from collections import Counter, OrderedDict
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from copy import deepcopy

SCHEMA = "polymarket_v7_native_exact_arb_observation_v2"
SAFETY = {"paper_only": True, "authenticated_execution": False,
          "real_order_submission": False, "real_capital_at_risk": False,
          "automatic_promotion": False, "execution_authority": False}
STAGES = ("raw", "after_fee", "after_reserve", "pre_allocation")
QUANTILES = (0, .0001, .001, .01, .05, .10, .25, .50, .75, .90, .95, .99)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def integer(row, key, minimum=0):
    value = row.get(key)
    if type(value) is not int or value < minimum or value > 2**63-1:
        raise ValueError("invalid_integer:"+key)
    return value


def boundary(row):
    if any(row.get(key) is not value for key, value in SAFETY.items()):
        raise ValueError("paper_boundary")


class Bundles:
    """Bounded off-path cache, content addressed exactly like the native loader."""
    def __init__(self, directory, model):
        self.directory, self.model, self.cache = Path(directory), model, OrderedDict()

    def relation(self, row):
        digest = row.get("native_bundle_sha256", "")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("bundle_identity")
        if digest not in self.cache:
            path = self.directory/(digest+".json")
            if path.stat().st_size > 4*1024*1024:
                raise ValueError("bundle_size")
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError("bundle_digest")
            body = json.loads(payload)
            boundary(body)
            if body.get("schema") != "polymarket_v7_exact_arb_native_bundle_v1" or body.get("model_sha") != self.model:
                raise ValueError("bundle_model_or_schema")
            relations = body.get("relations", [])
            if not 0 < len(relations) <= 512 or any(r.get("relation_handle") != i for i, r in enumerate(relations)):
                raise ValueError("bundle_handles")
            self.cache[digest] = body
            if len(self.cache) > 16:
                self.cache.popitem(last=False)
        self.cache.move_to_end(digest)
        body = self.cache[digest]
        if row.get("graph_generation") != body.get("graph_generation"):
            raise ValueError("graph_identity")
        handle = integer(row, "relation_handle")
        if handle >= len(body["relations"]):
            raise ValueError("relation_handle")
        relation = body["relations"][handle]
        if row.get("proof_handle") != int(relation["proof_hash"][:16], 16):
            raise ValueError("proof_identity")
        for key in ("economic_identity", "relation_family"):
            if not isinstance(relation.get(key), str) or not relation[key]:
                raise ValueError("relation_identity")
        if type(relation.get("sell_inventory")) is not bool:
            raise ValueError("relation_direction")
        return relation

    def document(self, row):
        self.relation(row)  # identical archive/model/handle admission for consumers
        return deepcopy(self.cache[row["native_bundle_sha256"]])


class Evidence:
    def __init__(self, database, bundles, model):
        self.db = sqlite3.connect(database)
        self.db.executescript("""
          CREATE TABLE seen(session TEXT, sequence INTEGER, digest TEXT,
                            PRIMARY KEY(session, sequence));
          CREATE TABLE distances(family TEXT, stage TEXT, pusd REAL, ticks REAL, bps REAL);
          CREATE TABLE episodes(id TEXT PRIMARY KEY, family TEXT, stage TEXT,
                                complete INTEGER, duration_ns INTEGER, payload TEXT);
        """)
        self.bundles, self.model = Bundles(bundles, model), model
        self.session = None
        self.closed_sessions = set()
        self.sequence = self.now = 0
        self.context = None
        self.states = {}
        self.counts = Counter()
        self.families = {}
        self.stream_hash = hashlib.sha256()

    def close(self):
        self.db.close()

    def _end(self, key, state, stage, reason, now=None):
        episode = state["episodes"].get(stage)
        if episode is None:
            return
        complete = reason == "OBSERVED_NONPOSITIVE" and not episode["left_censored"]
        end = now if reason == "OBSERVED_NONPOSITIVE" else episode["last_positive_ns"]
        row = {**episode, "end_reason": reason, "right_censored": reason != "OBSERVED_NONPOSITIVE",
               "end_ns": end, "observed_duration_lower_bound_ns": end-episode["first_positive_ns"],
               "complete_lifetime_ns": end-episode["first_positive_ns"] if complete else None}
        self.db.execute("INSERT INTO episodes VALUES (?,?,?,?,?,?)", (row["episode_id"], row["family"],
                        stage, int(complete), row["complete_lifetime_ns"], canonical(row)))
        state["episodes"][stage] = None

    def _censor(self, reason):
        for key, state in self.states.items():
            for stage in STAGES:
                self._end(key, state, stage, reason)
        self.states.clear()

    def ingest(self, row):
        boundary(row)
        if (row.get("schema") != SCHEMA or row.get("model_sha") != self.model
                or row.get("actionable") is not False or row.get("economic_execution_verified") is not False
                or row.get("evidence_scope") != "CAUSAL_FRAME_END_PRE_ALLOCATION_EVALUATION"):
            raise ValueError("observation_identity")
        session = row.get("observer_session_id")
        if not isinstance(session, str) or not session or len(session) > 256:
            raise ValueError("session_identity")
        seq = integer(row, "observation_sequence", 1)
        encoded = canonical(row)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        previous = self.db.execute("SELECT digest FROM seen WHERE session=? AND sequence=?", (session, seq)).fetchone()
        if previous:
            if previous[0] != digest:
                raise ValueError("conflicting_duplicate")
            self.counts["duplicate_rows_removed"] += 1
            return
        now = integer(row, "decision_start_ns", 1)
        receive = integer(row, "receive_monotonic_ns", 1)
        if receive > now or integer(row, "decision_end_ns", 1) < now:
            raise ValueError("observation_clock")
        relation = self.bundles.relation(row)
        if session != self.session:
            if session in self.closed_sessions:
                raise ValueError("interleaved_or_reordered_session")
            self._censor("SESSION_BOUNDARY")
            if self.session is not None:
                self.closed_sessions.add(self.session)
            self.session, self.sequence, self.now, self.context = session, 0, 0, None
        if seq <= self.sequence or now < self.now:
            raise ValueError("noncausal_input_order")
        if seq != self.sequence+1:
            self.counts["missing_observation_rows"] += seq-self.sequence-1
            self._censor("TELEMETRY_GAP")
        context = (row["native_bundle_sha256"], integer(row, "connection_epoch", 1),
                   integer(row, "continuity_serial", 1), row.get("selection_receipt_sha256"))
        if context != self.context:
            self._censor("GENERATION_OR_LINEAGE_BOUNDARY")
        self.context, self.sequence, self.now = context, seq, now
        self.db.execute("INSERT INTO seen VALUES (?,?,?)", (session, seq, digest))
        self.stream_hash.update((digest+"\n").encode())
        self.counts["evaluations"] += 1
        family = relation["relation_family"]+(":SELL" if relation["sell_inventory"] else ":BUY")
        totals = self.families.setdefault(family, Counter())
        prior_totals = totals.copy()
        totals["evaluations"] += 1
        key = (relation["economic_identity"], family)
        state = self.states.get(key)
        if state and now >= state["valid_until"]:
            for stage in STAGES:
                self._end(key, state, stage, "BOOK_OR_SOURCE_EXPIRED")
            state = None
        near = row.get("near_arbitrage", {})
        for field in ("books_ready", "lineage_ready", "fees_ready", "freshness_ready", "leg_skew_ready", "depth_complete"):
            if type(near.get(field)) is not bool:
                raise ValueError("missing_readiness:"+field)
            totals[field] += int(near[field])
        if type(near.get("valid")) is not bool or type(row.get("evaluation_accepted")) is not bool:
            raise ValueError("missing_evaluation_validity")
        totals["accepted_evaluations"] += int(row["evaluation_accepted"])
        sizing_unknown = row.get("rejection_reason") == "SIZING_INCOMPLETE" or (
            row.get("sizing_search_exhausted") is True and not row["evaluation_accepted"])
        if row.get("sizing_model") == "PER_L2_LEVEL_5DP_EXACT_ORDER_LATTICE_V1":
            for flag in ("global_size_optimum_proven", "sizing_search_exhausted"):
                if type(row.get(flag)) is not bool:
                    raise ValueError("invalid_sizing_certificate")
            if (row["global_size_optimum_proven"] and row["sizing_search_exhausted"]
                    or integer(row, "sizing_quantities_evaluated") > 4096
                    or row.get("sizing_proof_scope") != "RECORDED_MODEL_ONLY_NOT_VERIFIED_VENUE_EXECUTION"):
                raise ValueError("invalid_sizing_certificate")
            totals["sizing_model_evaluations"] += 1
            totals["sizing_model_optima_proven"] += int(row["global_size_optimum_proven"])
            totals["sizing_search_exhausted"] += int(row["sizing_search_exhausted"])
        totals["sizing_unknown_evaluations"] += int(sizing_unknown)
        if not near["valid"]:
            if row["evaluation_accepted"]:
                raise ValueError("accepted_without_valid_diagnostics")
            if state:
                for stage in STAGES:
                    self._end(key, state, stage, "INVALID_RELATION_OBSERVATION")
            self.states.pop(key, None)
            return family, totals-prior_totals
        if not all(near[field] for field in ("books_ready", "lineage_ready", "fees_ready", "freshness_ready", "leg_skew_ready")):
            raise ValueError("inconsistent_diagnostic_readiness")
        deadline = integer(row, "evaluation_valid_until_monotonic_ns", 1)
        if deadline < now or deadline > integer(row, "source_valid_until_monotonic_ns", 1):
            raise ValueError("evaluation_deadline")
        tick, guarantee = integer(near, "actionable_tick_nano", 1), integer(near, "guarantee_nano", 1)
        positives = {}
        for stage in STAGES[:3]:
            distance = integer(near, stage+"_distance_nano", -(2**63)+1)
            self.db.execute("INSERT INTO distances VALUES (?,?,?,?,?)",
                            (family, stage, distance/1e9, distance/tick, distance/guarantee*10000))
            positives[stage] = distance < 0
            totals[stage+"_positive_evaluations"] += int(distance < 0)
        positives["pre_allocation"] = row["evaluation_accepted"]
        if state is None:
            state = {"episodes": {}, "positive": {}, "valid_until": deadline}
            self.states[key] = state
        for stage, positive in positives.items():
            if stage == "pre_allocation" and sizing_unknown:
                self._end(key, state, stage, "SIZING_SEARCH_INCOMPLETE")
                state["positive"].pop(stage, None)
                continue
            episode = state["episodes"].get(stage)
            if positive and episode is None:
                left = stage not in state["positive"]
                identity = canonical([self.model, session, seq, key, stage])
                episode = {"schema": "polymarket_v7_native_exact_arb_diagnostic_episode_v1", **SAFETY,
                           "model_sha": self.model, "economic_execution_verified": False,
                           "episode_id": hashlib.sha256(identity.encode()).hexdigest(),
                           "family": family, "stage": stage, "economic_identity": relation["economic_identity"],
                           "observer_session_id": session, "first_observation_sequence": seq,
                           "native_bundle_sha256": row["native_bundle_sha256"],
                           "first_positive_ns": now, "last_positive_ns": now, "left_censored": left}
                state["episodes"][stage] = episode
                totals[stage+"_positive_segments"] += 1
                totals[stage+"_observed_starts"] += int(not left)
            if positive:
                episode["last_positive_ns"] = now
            elif episode:
                self._end(key, state, stage, "OBSERVED_NONPOSITIVE", now)
            state["positive"][stage] = positive
        state["valid_until"] = deadline
        return family, totals-prior_totals

    def finish(self):
        self._censor("END_OF_AVAILABLE_TAPE")
        for unit in ("pusd", "ticks", "bps"):
            self.db.execute("CREATE INDEX IF NOT EXISTS distance_"+unit+" ON distances(family,stage,"+unit+")")
        self.db.execute("CREATE INDEX IF NOT EXISTS episode_duration ON episodes(family,stage,complete,duration_ns)")
        self.db.commit()
        result = {"schema": "polymarket_v7_native_exact_arb_evidence_report_v1", **SAFETY,
                  "model_sha": self.model, "normalized_input_sha256": self.stream_hash.hexdigest(),
                  "tail_completeness_verified": False,
                  "near_arbitrage_scope": "TOP_OF_BOOK_PER_RELATION_UNIT_NOT_EXECUTABLE_CAPACITY",
                  "episode_scope": "OBSERVED_DIAGNOSTIC_SEGMENTS_NOT_FILLED_TRADES",
                  "engineering_evidence": dict(self.counts), "families": {},
                  "economic_evidence": {"fully_filled_opportunities": None, "net_pnl": None,
                                        "opportunities_per_hour": None, "research_decision": None},
                  "quantile_definition": "sorted_values[floor(p*(n-1))]; exact descriptive order statistics",
                  "limitations": ["Update-weighted diagnostics, not independent economic samples.",
                      "Pre-allocation acceptance is not execution, fillability or locked PnL.",
                      "Censored segments are not new opportunity arrivals or complete lifetimes.",
                      "No extrapolation to unobserved markets; causal coverage and terminal tape completeness unknown.",
                      "Content hashes bind native evidence; they are not independent settlement attestations."]}
        for family, counts in sorted(self.families.items()):
            metrics = {}
            for stage in STAGES[:3]:
                where, args = "family=? AND stage=?", (family, stage)
                count = self.db.execute("SELECT count(*) FROM distances WHERE "+where, args).fetchone()[0]
                metrics[stage] = {"observations": count, "quantiles": {
                    unit: self._quantiles("distances", unit, where, args, count) for unit in ("pusd", "ticks", "bps")},
                    "fraction_within_ticks": {str(t): self.db.execute(
                        "SELECT count(*) FROM distances WHERE "+where+" AND abs(ticks)<=?", (*args, t)).fetchone()[0]/count
                        if count else None for t in (.25, .5, 1, 2, 5)}}
            result["families"][family] = {"evaluation_funnel": dict(counts), "near_arbitrage": metrics,
                "lifetimes": {stage: self._lifetimes(family, stage) for stage in STAGES}}
        return result

    def _quantiles(self, table, column, where, args, count):
        # Floor-index order-statistic definition, no stochastic reservoir. SQL
        # identifiers here are private constants, never supplied by CLI/data.
        return {str(q): self.db.execute("SELECT "+column+" FROM "+table+" WHERE "+where+
                    " ORDER BY "+column+" LIMIT 1 OFFSET ?", (*args, int(q*(count-1)))).fetchone()[0]
                if count else None for q in QUANTILES}

    def _lifetimes(self, family, stage):
        where, args = "family=? AND stage=? AND complete=1", (family, stage)
        count = self.db.execute("SELECT count(*) FROM episodes WHERE "+where, args).fetchone()[0]
        total = self.db.execute("SELECT count(*) FROM episodes WHERE family=? AND stage=?", args).fetchone()[0]
        return {"complete_episodes": count, "censored_segments": total-count,
                "complete_lifetime_ns_quantiles": self._quantiles("episodes", "duration_ns", where, args, count)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--segments", nargs="+", required=True, type=Path)
    parser.add_argument("--bundles", required=True, type=Path)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--maximum-rows", type=int, default=20000000)
    args = parser.parse_args()
    if args.maximum_rows <= 0:
        parser.error("--maximum-rows must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="native-evidence-", dir=args.output) as temporary:
        evidence = Evidence(str(Path(temporary)/"evidence.sqlite"), args.bundles, args.model_sha)
        try:
            rows = 0
            for segment in args.segments:
                with segment.open("rb") as stream:
                    while True:
                        line = stream.readline(1024*1024+1)
                        if not line:
                            break
                        if len(line) > 1024*1024 or not line.endswith(b"\n"):
                            raise ValueError("oversize_or_unsealed_row")
                        rows += 1
                        if rows > args.maximum_rows:
                            raise ValueError("row_budget_exceeded")
                        evidence.ingest(json.loads(line))
            report = evidence.finish()
            # Publish one immutable report directory, so consumers cannot mix
            # episode and summary generations after a crash between writes.
            destination = args.output/hashlib.sha256(canonical(report).encode()).hexdigest()
            staging = Path(temporary)/"report"
            staging.mkdir()
            (staging/"report.json").write_text(canonical(report)+"\n")
            with (staging/"episodes.jsonl").open("w") as output:
                for (payload,) in evidence.db.execute("SELECT payload FROM episodes ORDER BY id"):
                    output.write(payload+"\n")
            if not destination.exists():
                staging.rename(destination)
            print(destination/"report.json")
        finally:
            evidence.close()


if __name__ == "__main__":
    main()
