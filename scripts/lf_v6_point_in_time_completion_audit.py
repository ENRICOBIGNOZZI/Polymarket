#!/usr/bin/env python3
from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Window:
    compatible_sell_volume: float
    queue_ahead: float
    target_shares: float
    entry_price: float
    unwind_bid: float


def point_in_time_fill(window: Window) -> bool:
    return window.compatible_sell_volume + 1e-12 >= window.queue_ahead + window.target_shares


def current_state_replay_fill(window: Window, *, current_queue_ahead: float) -> bool:
    return window.compatible_sell_volume + 1e-12 >= current_queue_ahead + window.target_shares


def unwind_loss(window: Window, *, shares: float | None = None, exit_bid: float | None = None) -> float:
    quantity = window.target_shares if shares is None else shares
    bid = window.unwind_bid if exit_bid is None else exit_bid
    return quantity * max(0.0, window.entry_price - bid)


def state_ev(
    windows: list[Window],
    *,
    complete_profit: float,
    current_queue_ahead: float | None = None,
    current_unwind_bid: float | None = None,
) -> float:
    pnl = []
    for window in windows:
        filled = (
            point_in_time_fill(window)
            if current_queue_ahead is None
            else current_state_replay_fill(window, current_queue_ahead=current_queue_ahead)
        )
        if filled:
            pnl.append(complete_profit)
        else:
            pnl.append(-unwind_loss(window, exit_bid=current_unwind_bid))
    return sum(pnl) / len(pnl)


def audit() -> dict[str, object]:
    windows = [
        Window(compatible_sell_volume=50.0, queue_ahead=90.0, target_shares=10.0, entry_price=0.40, unwind_bid=0.20),
        Window(compatible_sell_volume=50.0, queue_ahead=10.0, target_shares=10.0, entry_price=0.40, unwind_bid=0.39),
    ]
    truth = [point_in_time_fill(window) for window in windows]
    replay_low_queue = [current_state_replay_fill(window, current_queue_ahead=10.0) for window in windows]
    replay_high_queue = [current_state_replay_fill(window, current_queue_ahead=90.0) for window in windows]

    complete_profit = 1.0
    true_ev = state_ev(windows, complete_profit=complete_profit)
    current_low_queue_current_good_bid_ev = state_ev(
        windows,
        complete_profit=complete_profit,
        current_queue_ahead=10.0,
        current_unwind_bid=0.39,
    )
    current_high_queue_current_good_bid_ev = state_ev(
        windows,
        complete_profit=complete_profit,
        current_queue_ahead=90.0,
        current_unwind_bid=0.39,
    )

    return {
        "schema": "lf_v6_point_in_time_completion_audit_v1",
        "finding": "historical completion and unwind EV are not identified from tape plus one current book snapshot",
        "windows": [asdict(window) for window in windows],
        "point_in_time_fill_states": truth,
        "point_in_time_completion_rate": sum(truth) / len(truth),
        "replay_with_current_queue_10": replay_low_queue,
        "replay_completion_rate_current_queue_10": sum(replay_low_queue) / len(replay_low_queue),
        "replay_with_current_queue_90": replay_high_queue,
        "replay_completion_rate_current_queue_90": sum(replay_high_queue) / len(replay_high_queue),
        "true_point_in_time_mean_ev": true_ev,
        "replay_mean_ev_current_queue_10_current_bid_039": current_low_queue_current_good_bid_ev,
        "replay_mean_ev_current_queue_90_current_bid_039": current_high_queue_current_good_bid_ev,
        "required_repair": [
            "persist point-in-time queue/depth and executable quote state at each candidate/window origin",
            "persist or reconstruct point-in-time unwind bid/depth and fees at each abort horizon",
            "estimate joint fill states from same-window point-in-time state, or use forward-only shadow observations",
            "do not reuse the current order book to label historical windows",
        ],
        "decision": "MORE_EVIDENCE_REQUIRED",
    }


def main() -> int:
    print(json.dumps(audit(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
