import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v7_causal_book import BookTimeline, SCHEMA, TARGET
from v7_external_lead_lag_train import train, load_rows
from v7_external_lead_lag_collector import Collector
from v7_fair_model_artifact import canonical_hash

SHA = "a" * 40


class CausalBookTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "current.jsonl"
        self.book = BookTimeline(self.path, SHA)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.book.close)
        self.base = time.time_ns() // 1000000 - 5000
        self.sequence = 0

    def row(self, token, offset, mid=.5, **changes):
        self.sequence += 1
        return {"schema": SCHEMA, "model_sha": SHA, "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "observer_session_id": "session", "connection_epoch": 1,
            "observer_sequence": self.sequence, "market_id": "market", "token_id": token,
            "receive_wall_ms": self.base + offset, "exchange_event_ns": (self.base+offset)*1000000,
            "valid": True, "lineage_continuous": True,
            "best_bid": mid-.01, "best_ask": mid+.01, "tick_size": .01, **changes}

    def status(self, **changes):
        return {"model_sha": SHA, "state": "running", "evidence_complete": True,
            "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
            "observer_session_id": self.book.session, "connection_epoch": self.book.epoch,
            "timestamp_ms": time.time_ns()//1000000, "book_events_written": self.book.sequence,
            "book_watermark_receive_wall_ms": self.book.watermark_ms, **changes}

    def label(self, **status):
        return self.book.label("market", "yes", "no", self.base+10, self.base+110, self.status(**status))

    def seed(self):
        for row in [self.row("yes", 0), self.row("no", 0),
                    self.row("yes", 100, .6), self.row("no", 100, .4),
                    self.row("yes", 120, .8), self.row("no", 120, .2)]:
            self.book.ingest(row)

    def test_asof_excludes_prices_received_after_target(self):
        self.seed()
        evidence = self.label()
        self.assertAlmostEqual(evidence["origin_pm_yes"], .5)
        self.assertAlmostEqual(evidence["label_pm_yes"], .6)
        self.assertEqual(evidence["label_pm_receive_ts_ms"], self.base+100)
        self.assertEqual(evidence["label_available_after_receive_ms"], self.base+120)

    def test_status_cannot_invent_market_progress_or_hide_consumer_gap(self):
        self.seed()
        for overrides in ({"book_events_written": 1000}, {"evidence_complete": False},
                          {"connection_epoch": 2}, {"timestamp_ms": 1},
                          {"book_watermark_receive_wall_ms": self.base+100},
                          {"book_events_written": "broken"}):
            self.assertIsNone(self.label(**overrides))
        self.book.watermark_ms = self.base+100
        self.assertIsNone(self.label(book_watermark_receive_wall_ms=self.base+10000))

    def test_gap_reconnect_invalid_book_and_complement_disagreement_are_censored(self):
        self.seed()
        self.book.ingest(self.row("yes", 130, observer_sequence=99))
        self.assertIsNone(self.label())
        self.seed()
        self.book.ingest(self.row("yes", 140, connection_epoch=2))
        self.assertIsNone(self.label())
        self.book.invalidate()
        for row in [self.row("yes", 0), self.row("no", 0),
                    self.row("yes", 100, valid=False), self.row("no", 120)]: self.book.ingest(row)
        self.assertIsNone(self.label())
        self.book.invalidate()
        for row in [self.row("yes", 0), self.row("no", 0),
                    self.row("yes", 100, .8), self.row("no", 120)]: self.book.ingest(row)
        self.assertIsNone(self.label())

    def test_quiet_book_is_valid_only_with_continuous_stream_watermark(self):
        for row in [self.row("yes", 0), self.row("no", 0), self.row("unrelated", 120)]: self.book.ingest(row)
        self.assertAlmostEqual(self.label()["label_pm_yes"], .5)

    def test_partial_lines_and_rotation_preserve_sequence(self):
        first, second = self.row("yes", 0), self.row("no", 0)
        self.path.write_text(json.dumps(first)+"\n"+json.dumps(second))
        self.book.poll()
        self.assertEqual(self.book.sequence, 1)
        with self.path.open("a") as stream: stream.write("\n")
        self.path.rename(self.path.with_name("session.segment-1000000.jsonl"))
        self.path.write_text(json.dumps(self.row("unrelated", 120))+"\n")
        self.book.poll()
        self.assertEqual(self.book.sequence, 3)
        self.assertAlmostEqual(self.label()["label_pm_yes"], .5)

    def test_trainer_rejects_mixed_targets(self):
        with self.assertRaisesRegex(ValueError, "mixed_or_unknown"):
            train([{}, {"target_semantics": TARGET}], SHA)

    def test_collector_labels_even_when_router_snapshot_has_not_changed(self):
        from test_v7_external_lead_lag import fair_origin, router
        root = Path(self.directory.name)
        fair = fair_origin()
        fair["market"].update(market_id="market", yes_token="yes", no_token="no")
        cut = fair["fair"]["rich_feature_cut"]
        cut["observed_wall_ns"] = (self.base+10)*1000000
        cut["market_prior_snapshot"]["receive_ts_ms"] = self.base
        fair["fair"]["rich_feature_sha256"] = canonical_hash(cut)
        route = router(self.base, .5, "unchanged")
        route["live_market"]["market_id"] = "market"
        (root/"fair.json").write_text(json.dumps(fair))
        (root/"router.json").write_text(json.dumps(route))
        rows = [self.row("yes",0), self.row("no",0), self.row("yes",100,.6), self.row("no",100,.4)]
        rows.append(self.row("unrelated",1200))
        self.path.write_text("".join(json.dumps(row)+"\n" for row in rows))
        self.book.poll()
        (root/"book-status.json").write_text(json.dumps(self.status()))
        collector = Collector(root/"fair.json", root/"router.json", root/"labels.jsonl", root/"status.json",
                              SHA, book_tape=self.path, book_status=root/"book-status.json")
        self.addCleanup(collector.book.close)
        collector.last_router_snapshot_id = "unchanged"
        collector.tick()
        labels = [json.loads(line) for line in (root/"labels.jsonl").read_text().splitlines()]
        self.assertEqual(len(labels), 4)
        self.assertTrue(all(row["nominal_horizon_eligible"] for row in labels))
        self.assertTrue(all(row["target_semantics"] == TARGET for row in labels))
        self.assertEqual(load_rows([root/"labels.jsonl"], SHA), [])
        self.assertEqual(len(load_rows([root/"labels.jsonl"], SHA, TARGET)), 4)
        collector.tick()
        self.assertEqual(collector.labels, 4)


if __name__ == "__main__": unittest.main()
