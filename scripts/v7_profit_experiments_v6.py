"""Prospective v6 profit experiment extensions.

Maker orders become confirmatory anchors only after arrival-time causal evidence
passes a frozen eligibility rule. Skips are durable audit records and never use
future fills, markouts, or settlements.
"""
from __future__ import annotations

import time

from v7_maker_anchor_eligibility import evaluate_anchor
from v7_profit_experiments import ProfitExperiments, rows


class ProspectiveProfitExperiments(ProfitExperiments):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.maker_candidates = {}
        self.maker_skipped_order_ids = set()
        for row in rows(self.output / "observations.jsonl"):
            if row.get("kind") == "MAKER_ANCHOR_SKIP" and row.get("order_record_id"):
                self.maker_skipped_order_ids.add(str(row["order_record_id"]))
        maker = self.protocol.get("maker") if isinstance(self.protocol.get("maker"), dict) else {}
        self.anchor_eligibility_wait_ms = int(maker.get("anchor_eligibility_wait_ms") or 1000)
        if not 100 <= self.anchor_eligibility_wait_ms <= 5000:
            raise ValueError("maker anchor eligibility wait must be within 100..5000ms")

    def _candidate_order(self, order):
        if not self.accept_new_anchors:
            return None
        metadata = order.get("metadata") or {}
        market = str(order.get("market_id") or "")
        order_record_id = str(order.get("record_id") or "")
        if (
            order.get("event_type") != "ORDER_SUBMITTED"
            or metadata.get("component") != "professional_maker"
            or not market
            or market in self.anchors
            or market in self.maker_candidates
            or order_record_id in self.maker_skipped_order_ids
            or order.get("model_sha") != self.sha
            or order.get("paper_only") is not True
            or order.get("authenticated_execution") is not False
            or metadata.get("counterfactual") is True
            or metadata.get("excluded_from_portfolio_equity") is True
        ):
            return None
        origin_ns = int(order.get("receive_ts_ms") or 0) * 1_000_000
        if origin_ns < self.manifest["forward_start_ns"]:
            return None
        end_ns = self.manifest.get("confirmatory_end_ns")
        if end_ns is not None and origin_ns >= int(end_ns):
            return None
        if self.protocol["maker"].get("validity_semantics") == "SEPARATE_EXECUTION_AND_MARKOUT_WINDOWS":
            actual_model = ((metadata.get("opportunity_envelope") or {}).get("settlement_model") or {}).get("model_hash")
            if actual_model != self.manifest["frozen_model_hash"]:
                return None
        return market, order_record_id

    def collect_anchors(self, now):
        status_path = self.run_root / "micro_maker" / "fillability_ws_status.json"
        try:
            import json
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
        for order in self.ledger.poll():
            identity = self._candidate_order(order)
            if identity is None:
                continue
            market, order_record_id = identity
            self.maker_candidates[market] = {
                "order": order,
                "order_record_id": order_record_id,
                "first_seen_ns": now,
            }

        for market, candidate in list(self.maker_candidates.items()):
            order = candidate["order"]
            eligibility = evaluate_anchor(
                order, self.book, status, self.protocol, now_ms=now // 1_000_000,
            )
            state = eligibility["state"]
            elapsed_ms = (now - candidate["first_seen_ns"]) / 1_000_000
            if state == "PENDING" and elapsed_ms < self.anchor_eligibility_wait_ms:
                continue
            if state != "ELIGIBLE":
                reason = eligibility["reason"] if state == "INELIGIBLE" else "ELIGIBILITY_TIMEOUT:" + eligibility["reason"]
                self.emit(
                    "MAKER_ANCHOR_SKIP",
                    market_id=market,
                    token_id=order.get("token_id"),
                    origin_ms=order.get("receive_ts_ms"),
                    order_record_id=candidate["order_record_id"],
                    eligibility_state=state,
                    eligibility_reason=reason,
                    eligibility_semantics="ARRIVAL_TIME_ONLY_NO_FUTURE_OUTCOME",
                )
                self.maker_skipped_order_ids.add(candidate["order_record_id"])
                del self.maker_candidates[market]
                continue

            self.anchors.add(market)
            row = self.emit(
                "MAKER_ANCHOR",
                market_id=market,
                token_id=order["token_id"],
                origin_ms=order["receive_ts_ms"],
                order=order,
                book_gap_counter=eligibility["book_gap_counter"],
                observer_session_id=eligibility["observer_session_id"],
                connection_epoch=eligibility["connection_epoch"],
                anchor_eligibility={
                    "state": "ELIGIBLE",
                    "reason": eligibility["reason"],
                    "feature_age_ms": eligibility["feature_age_ms"],
                    "semantics": "ARRIVAL_TIME_ONLY_NO_FUTURE_OUTCOME",
                },
            )
            self.maker_pending[market] = row
            del self.maker_candidates[market]

    def seal_confirmatory_window(self, now):
        end = self.manifest.get("confirmatory_end_ns")
        if end is not None and any(
            int(item["order"].get("receive_ts_ms") or 0) * 1_000_000 < int(end)
            for item in self.maker_candidates.values()
        ):
            return
        return super().seal_confirmatory_window(now)
