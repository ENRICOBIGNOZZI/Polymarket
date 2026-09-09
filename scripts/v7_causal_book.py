"""Receive-time book history from the canonical observer; no execution owner."""
from collections import defaultdict, deque
import json
import math
import time
from pathlib import Path

SCHEMA = "polymarket_v7_causal_book_observation_v1"
TARGET = "CAUSAL_BOOK_STATE_AT_HORIZON"


class BookTimeline:
    def __init__(self, path: Path, model_sha: str, retention_ms: int = 10000):
        self.path, self.model_sha = path, model_sha
        self.retention_ms = retention_ms
        self.handle = None
        self.history = defaultdict(lambda: deque(maxlen=20000))
        self.session = ""
        self.epoch = 0
        self.sequence = 0
        self.watermark_ms = 0
        self.watermark_monotonic_ns = 0
        self.gaps = 0

    def invalidate(self):
        self.history.clear()
        self.watermark_ms = 0
        self.watermark_monotonic_ns = 0
        self.gaps += 1

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def ingest(self, row):
        if (not isinstance(row, dict) or row.get("schema") != SCHEMA
                or row.get("model_sha") != self.model_sha
                or row.get("paper_only") is not True
                or row.get("authenticated_execution") is not False
                or row.get("real_order_submission") is not False
                or row.get("execution_authority") != "ZERO_AUTHORITY_RESEARCH_ONLY"):
            self.invalidate()
            return
        try:
            session = str(row["observer_session_id"])
            epoch, sequence = int(row["connection_epoch"]), int(row["observer_sequence"])
            received = int(row["receive_wall_ms"])
            monotonic = int(row.get('receive_monotonic_ns') or 0)
            exchange_ns = int(row["exchange_event_ns"])
            token, market = str(row["token_id"]), str(row["market_id"])
            if not session or not token or not market or min(epoch, sequence, received) <= 0:
                raise ValueError("identity")
        except (KeyError, TypeError, ValueError, OverflowError):
            self.invalidate()
            return
        if ((session, epoch) != (self.session, self.epoch)
                or sequence != self.sequence + 1 or received < self.watermark_ms
                or monotonic < self.watermark_monotonic_ns):
            self.invalidate()
        self.session, self.epoch, self.sequence = session, epoch, sequence
        self.watermark_ms = received
        self.watermark_monotonic_ns = monotonic
        row = {**row, "receive_wall_ms": received, "exchange_event_ns": exchange_ns,
               "observer_sequence": sequence, "connection_epoch": epoch}
        history = self.history[(market, token)]
        history.append(row)
        # Keep the last observation preceding the rolling window as well.
        while len(history) > 1 and history[1]["receive_wall_ms"] < received - self.retention_ms:
            history.popleft()

    def poll(self):
        """Drain the old inode before following a producer-owned segment roll."""
        for _ in range(2):
            if self.handle is None:
                try: self.handle = self.path.open("rb")
                except OSError: return
            while True:
                offset = self.handle.tell()
                raw = self.handle.readline()
                if not raw or not raw.endswith(b"\n"):
                    self.handle.seek(offset)
                    break
                try: self.ingest(json.loads(raw))
                except (ValueError, UnicodeDecodeError): self.invalidate()
            try:
                import os
                old, current = os.fstat(self.handle.fileno()), self.path.stat()
                if (old.st_dev, old.st_ino) == (current.st_dev, current.st_ino):
                    if current.st_size < self.handle.tell():
                        self.invalidate(); self.handle.seek(0)
                    return
            except OSError: return
            self.handle.close(); self.handle = None

    def asof(self, market, token, timestamp_ms):
        for row in reversed(self.history.get((market, token), ())):
            if row["receive_wall_ms"] <= timestamp_ms:
                if row.get("valid") is not True or row.get("lineage_continuous") is not True:
                    return None
                try:
                    bid, ask = float(row["best_bid"]), float(row["best_ask"])
                    if not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid < ask < 1): return None
                except (TypeError, KeyError, ValueError): return None
                return row
        return None

    def label(self, market, yes_token, no_token, origin_ms, target_ms, status):
        # A status publication cannot stand in for a processed market event.
        # The observer watermark and locally consumed sequence must both cover
        # the target, with no missing frames, dropped queue entries or reconnect.
        try:
            published = int(status.get("timestamp_ms") or 0)
            written = int(status.get("book_events_written") or 0)
            watermark = int(status.get("book_watermark_receive_wall_ms") or 0)
        except (ValueError, TypeError, OverflowError): return None
        now = time.time_ns() // 1000000
        if (not 0 < origin_ms <= target_ms <= now or yes_token == no_token
                or not 0 <= now - published <= 2000
                or status.get("paper_only") is not True
                or status.get("authenticated_execution") is not False
                or status.get("real_order_submission") is not False
                or status.get("model_sha") != self.model_sha or status.get("state") != "running"
                or status.get("evidence_complete") is not True
                or status.get("observer_session_id") != self.session
                or status.get("connection_epoch") != self.epoch
                or written > self.sequence
                or min(self.watermark_ms, watermark) < target_ms):
            return None
        origin = [self.asof(market, token, origin_ms) for token in (yes_token, no_token)]
        future = [self.asof(market, token, target_ms) for token in (yes_token, no_token)]
        if any(row is None for row in origin + future): return None
        def probability(pair):
            yes = (float(pair[0]["best_bid"]) + float(pair[0]["best_ask"])) / 2
            no = (float(pair[1]["best_bid"]) + float(pair[1]["best_ask"])) / 2
            # Complement inconsistency is censored, not forced into a price.
            tolerance = max(float(row.get("tick_size") or 0) for row in pair)
            if not math.isfinite(tolerance) or not 0 < tolerance < 1: return None
            if abs(yes + no - 1) > 2 * tolerance + 1e-9: return None
            return (yes + 1 - no) / 2
        try: p0, p1 = probability(origin), probability(future)
        except (ValueError, TypeError, OverflowError): return None
        if p0 is None or p1 is None: return None
        return {"origin_pm_yes": p0, "label_pm_yes": p1,
                "origin_pm_snapshot_id": ":".join(str(row["observer_sequence"]) for row in origin),
                "origin_pm_receive_ts_ms": max(row["receive_wall_ms"] for row in origin),
                "origin_book_cuts": origin, "label_book_cuts": future,
                "label_available_after_receive_ms": self.watermark_ms,
                "label_pm_snapshot_id": ":".join(str(row["observer_sequence"]) for row in future),
                "label_pm_receive_ts_ms": max(row["receive_wall_ms"] for row in future),
                "label_pm_exchange_ts_ms": max(int(row["exchange_event_ns"]) // 1000000 for row in future),
                "observer_session_id": self.session, "connection_epoch": self.epoch}
