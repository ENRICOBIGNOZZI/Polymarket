from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from v7_maker_microstructure import summarize_maker_microstructure

SHA = "a" * 40


def emit(path: Path, **row):
    base = {
        "schema_version": 1,
        "strategy": "MICRO_MAKER_PRO",
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "recorded_ts_ms": 1000,
    }
    base.update(row)
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(base) + "\n")


def candidate(path: Path, cid: str, market: str, token: str, outcome: str, side: str, action: str, bid: float, ask: float, bd: float, ad: float, tox: float, ts: int, lifetime_arm: str | None = None):
    emit(
        path,
        event_type="CANDIDATE",
        record_id=f"c-{cid}",
        candidate_id=cid,
        market_id=market,
        token_id=token,
        side=side,
        intended_action=action,
        limit_price=bid if side == "BUY" else ask,
        bid=bid,
        ask=ask,
        bid_depth=bd,
        ask_depth=ad,
        receive_ts_ms=ts,
        decision_ts_ms=ts,
        metadata={
            "outcome": outcome,
            "toxicity": tox,
            **({"exploration_lifetime_arm": lifetime_arm} if lifetime_arm else {}),
        },
    )


def order(path: Path, cid: str, oid: str, market: str, token: str, outcome: str, side: str, action: str, px: float, size: float, queue: float, bid: float, ask: float, bd: float, ad: float, tox: float, ts: int):
    emit(
        path,
        event_type="ORDER_SUBMITTED",
        record_id=f"o-{oid}",
        candidate_id=cid,
        order_id=oid,
        market_id=market,
        token_id=token,
        side=side,
        intended_action=action,
        limit_price=px,
        intended_size=size,
        queue_ahead=queue,
        bid=bid,
        ask=ask,
        bid_depth=bd,
        ask_depth=ad,
        receive_ts_ms=ts,
        decision_ts_ms=ts,
        metadata={"outcome": outcome, "toxicity": tox},
    )


def fill(path: Path, fid: str, oid: str, market: str, token: str, side: str, px: float, size: float, ts: int):
    emit(
        path,
        event_type="FILL",
        record_id=f"f-{fid}",
        fill_id=fid,
        order_id=oid,
        market_id=market,
        token_id=token,
        side=side,
        fill_price=px,
        filled_size=size,
        receive_ts_ms=ts,
    )


def markout(path: Path, fid: str, oid: str, market: str, token: str, side: str, horizon: str, value: float, ts: int):
    emit(
        path,
        event_type="MARKOUT",
        record_id=f"m-{fid}-{horizon}",
        fill_id=fid,
        order_id=oid,
        market_id=market,
        token_id=token,
        side=side,
        receive_ts_ms=ts,
        markouts={horizon: value},
    )


class MakerMicrostructureTests(unittest.TestCase):
    def test_chain_markout_and_realized_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger = tmp_path / "execution.jsonl"
            rewards = tmp_path / "reward_selection.json"
            rewards.write_text(json.dumps({"markets": [{"market_id": "M1", "reward_intensity": 2.0}]}))

            candidate(ledger, "c1", "M1", "YES", "YES", "BUY", "JOIN", .39, .41, 100, 80, .10, 1000)
            order(ledger, "c1", "o1", "M1", "YES", "YES", "BUY", "JOIN", .40, 10, 5, .39, .41, 100, 80, .10, 1000)
            fill(ledger, "f1", "o1", "M1", "YES", "BUY", .40, 10, 1100)
            markout(ledger, "f1", "o1", "M1", "YES", "BUY", "10s", .02, 11100)

            candidate(ledger, "c2", "M1", "NO", "NO", "BUY", "FADE1", .49, .51, 90, 110, .20, 1200)
            order(ledger, "c2", "o2", "M1", "NO", "NO", "BUY", "FADE1", .50, 10, 30, .49, .51, 90, 110, .20, 1200)
            fill(ledger, "f2", "o2", "M1", "NO", "BUY", .50, 10, 1300)
            markout(ledger, "f2", "o2", "M1", "NO", "BUY", "10s", -.01, 11300)
            emit(
                ledger,
                event_type="FINAL",
                record_id="final-1",
                market_id="M1",
                final_pnl=1.0,
                metadata={"complete_set_merge": True, "merged_shares": 10.0},
            )

            out = summarize_maker_microstructure(ledger, rewards, use_cache=False)
            assert out["orders"] == 2
            assert out["filled_orders"] == 2
            assert out["fills"] == 2
            assert out["markouts"]["10s"] == 2
            assert abs(out["realized_pnl"] - 1.0) < 1e-12
            assert abs(out["attributed_realized_pnl"] - 1.0) < 1e-12
            assert out["quality"]["linked_fills"] == 2
            assert out["quality"]["linked_markouts"] == 2

            total = next(x for x in out["segments"] if x["action"] == "ALL" and x["dimension"] == "all")
            assert total["orders"] == 2
            assert total["filled_orders"] == 2
            assert abs(total["markout_pnl"]["10s"] - (10 * .02 + 10 * -.01)) < 1e-12
            assert abs(total["markout_shares"]["10s"] - 20) < 1e-12
            assert abs(total["realized_pnl"] - 1.0) < 1e-12

            reward_rows = [x for x in out["segments"] if x["dimension"] == "reward"]
            assert reward_rows and all(x["bucket"] == "REWARDED" for x in reward_rows)

    def test_lifetime_experiment_arm_is_preserved_through_fill_and_markout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "execution.jsonl"
            candidate(
                ledger, "c1", "M1", "YES", "YES", "BUY", "IMPROVE1",
                .39, .42, 10, 10, .1, 1000, lifetime_arm="PERSISTENT",
            )
            order(
                ledger, "c1", "o1", "M1", "YES", "YES", "BUY", "IMPROVE1",
                .40, 2, 0, .39, .42, 10, 10, .1, 1000,
            )
            fill(ledger, "f1", "o1", "M1", "YES", "BUY", .40, 2, 1100)
            markout(ledger, "f1", "o1", "M1", "YES", "BUY", "10s", .01, 11100)

            out = summarize_maker_microstructure(ledger, use_cache=False)
            arm = next(
                row for row in out["segments"]
                if row["dimension"] == "lifetime_arm" and row["bucket"] == "PERSISTENT"
            )
            assert arm["orders"] == 1
            assert arm["filled_orders"] == 1
            assert arm["markout_count"]["10s"] == 1
            assert out["quality"]["lifetime_arm_known_orders"] == 1

    def test_sell_average_cost_realized_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger = tmp_path / "execution.jsonl"
            rewards = tmp_path / "reward_selection.json"
            rewards.write_text('{"markets": []}')

            candidate(ledger, "c1", "M1", "YES", "YES", "BUY", "JOIN", .39, .41, 10, 10, .1, 1000)
            order(ledger, "c1", "o1", "M1", "YES", "YES", "BUY", "JOIN", .40, 10, 0, .39, .41, 10, 10, .1, 1000)
            fill(ledger, "f1", "o1", "M1", "YES", "BUY", .40, 10, 1100)

            candidate(ledger, "c2", "M1", "YES", "YES", "SELL", "IMPROVE1", .49, .51, 10, 10, .2, 1200)
            order(ledger, "c2", "o2", "M1", "YES", "YES", "SELL", "IMPROVE1", .50, 5, 0, .49, .51, 10, 10, .2, 1200)
            fill(ledger, "f2", "o2", "M1", "YES", "SELL", .50, 5, 1300)

            out = summarize_maker_microstructure(ledger, rewards, use_cache=False)
            assert abs(out["realized_pnl"] - .5) < 1e-12
            improve = next(x for x in out["segments"] if x["action"] == "IMPROVE" and x["dimension"] == "all")
            assert abs(improve["realized_pnl"] - .5) < 1e-12


if __name__ == "__main__":
    unittest.main()
