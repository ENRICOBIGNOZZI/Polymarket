from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from research.walk_forward_v3.btc_compact_equity import stream_raw_sessions

from research.walk_forward_v3.maker_a0_a5_2h import (
    Ridge, external_feature, external_fair_feature, pm_feature, full_execution_feature, feature_dict, split_60_40,
    load_external_venue_csvs, external_venue_features, oriented_pm_anchor_features,
    load_feature_anchor_rows, load_book_anchor_rows, recent_trade_flow,
    build_static_market_metadata, WINDOW_NS,
)


class MakerA0A5Tests(unittest.TestCase):
    def test_feature_families_are_interpretable(self):
        self.assertTrue(external_feature("binance_return_100ms_bp"))
        self.assertTrue(external_feature("tape.external.dispersion_bps"))
        self.assertFalse(external_feature("tape.pm_yes_imbalance"))
        self.assertTrue(pm_feature("tape.pm_yes_imbalance"))
        self.assertTrue(pm_feature("short_return_ticks"))
        self.assertFalse(pm_feature("tape.external.depth_imbalance"))
        self.assertFalse(pm_feature("external.trade_intensity"))
        self.assertTrue(full_execution_feature("queue_ahead"))
        self.assertTrue(full_execution_feature("distance_to_reference_bp"))
        self.assertFalse(full_execution_feature("realized_markout_1s"))

    def test_external_fair_features_are_external_only(self):
        self.assertTrue(external_fair_feature("tape.external.spot_minus_oracle_bp"))
        self.assertTrue(external_fair_feature("tape.deribit.implied_vol"))
        self.assertTrue(external_fair_feature("external.binance_return_100ms_bp"))
        self.assertFalse(external_fair_feature("tape.pm_yes_mid"))
        self.assertFalse(external_fair_feature("pm.yes_bid_depth_l1"))
        self.assertFalse(external_fair_feature("execution.yes_queue_ahead"))

    def test_external_feature_view_excludes_pm_context(self):
        row={
            "pair":{"pm_yes":0.73},
            "tte_ns":60_000_000_000,
            "signal_age_ns":12_000_000,
            "asset":"BTC","horizon":"M5",
            "features":{"binance_return_100ms_bp":1.2,"tape.pm_yes_imbalance":0.4},
        }
        external=feature_dict(
            row,["binance_return_100ms_bp"],
            include_pm=False,include_signal_age=False)
        pm=feature_dict(
            row,["tape.pm_yes_imbalance"],
            include_pm=True,include_signal_age=False)
        self.assertNotIn("ctx.pm_yes",external)
        self.assertNotIn("ctx.signal_age_ms",external)
        self.assertEqual(external["binance_return_100ms_bp"],1.2)
        self.assertAlmostEqual(pm["ctx.pm_yes"],0.73)

    def test_receive_time_venue_features_use_same_epoch_and_backward_asof(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"btc.csv"
            path.write_text(
                "1000000000,1,2,7,100.0\n"
                "1100000000,1,2,7,101.0\n"
                "1000000000,2,2,8,100.0\n"
                "1100000000,2,2,8,100.5\n"
                "1000000000,3,2,9,100.0\n"
                "1100000000,3,2,9,99.5\n",
                encoding="utf-8")
            index,diag=load_external_venue_csvs([f"BTC={path}"])
            features=external_venue_features(index,"BTC",1_100_000_000)
            self.assertGreater(features["external.binance_return_100ms_bp"],0)
            self.assertGreater(features["external.coinbase_return_100ms_bp"],0)
            self.assertLess(features["external.bybit_return_100ms_bp"],0)
            self.assertIn("external.cross_venue_agreement_100ms",features)
            self.assertEqual(diag["accepted"],6)

    def test_pm_anchor_features_orient_no_toward_yes_probability(self):
        raw={
            "placement_features":{
                "imbalance":0.5,"ofi":2.0,"short_return_ticks":1.0,
                "spread_ticks":1.0,"ew_vol_ticks":2.0,
                "aggressive_buy_prints_per_second":4.0,
                "aggressive_sell_prints_per_second":1.0,
            },
            "bid_depth_l1":12.0,"ask_depth_l1":4.0,
            "bid_levels_l10":[{"size":2.0} for _ in range(5)],
            "ask_levels_l10":[{"size":1.0} for _ in range(5)],
        }
        yes=oriented_pm_anchor_features(raw,outcome="YES")
        no=oriented_pm_anchor_features(raw,outcome="NO")
        self.assertAlmostEqual(yes["pm.anchor_imbalance"],0.5)
        self.assertAlmostEqual(no["pm.anchor_imbalance"],-0.5)
        self.assertAlmostEqual(no["pm.anchor_ofi"],-2.0)
        self.assertGreater(yes["pm.anchor_l5_depth_imbalance"],0)
        self.assertLess(no["pm.anchor_l5_depth_imbalance"],0)
        self.assertEqual(yes["pm.anchor_spread_ticks"],no["pm.anchor_spread_ticks"])

    def test_ridge_fits_simple_relation(self):
        rows=[{"x":float(i)} for i in range(100)]
        y=[2.0*float(i)+1.0 for i in range(100)]
        model=Ridge(["x"],ridge=1e-6).fit(rows,y)
        self.assertAlmostEqual(model.predict({"x":12.0}),25.0,places=3)

    def test_feature_tape_drives_quote_timing_not_native_signal_rows(self):
        import json,tempfile
        native=[{
            "market_id":"m1","asset":"BTC","horizon":"M5",
            "fee_rate":0.07,"fee_exponent":1.0,"minimum":5.0,
        }]
        market_meta,context_meta=build_static_market_metadata(native)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"feature_tape.jsonl"
            rows=[]
            for ns in (1_000_000_000,1_100_000_000,1_600_000_000):
                record={
                    "schema":"polymarket_v7_multi_crypto_feature_tape_v1",
                    "decision_wall_ns":ns,"available_at_ns":ns-1,
                    "model_sha":"a"*40,
                    "asset":"BTC","horizon":"M5","market_id":"m1",
                    "yes_token":"y","no_token":"n","active_now":True,
                    "features":{"tte_seconds":100.0,"pm_book_valid":True,
                                "external":{"return_100ms_bp":1.0}},
                    "paper_only":True,"authenticated_execution":False,
                    "real_order_submission":False,"execution_authority":False,
                }
                rows.append(record)
            p.write_text("".join(json.dumps(r)+"\n" for r in rows),encoding="utf-8")
            anchors,diag=load_feature_anchor_rows(
                [p],minimum_wall_ns=0,market_meta=market_meta,context_meta=context_meta)
        self.assertEqual(len(anchors),2)
        self.assertEqual([r["decision_ns"] for r in anchors],[1_000_000_000,1_600_000_000])
        self.assertEqual(anchors[0]["fee_rate"],0.07)
        self.assertIn("tape.external.return_100ms_bp",anchors[0]["features"])
        self.assertEqual(diag["anchor_cadence_ms"],500)

    def test_book_anchor_clock_is_exact_grid_not_event_time(self):
        import json,tempfile
        metadata={
            "m1":{
                "market_id":"m1","asset":"BTC","horizon":"M5",
                "yes_token":"y","no_token":"n",
                "start_timestamp_ms":1000,"end_timestamp_ms":3000,
                "fee_rate":0.07,"fee_exponent":1.0,"minimum":5.0,
            }
        }
        def record(ms,seq):
            return {
                "schema":"polymarket_v7_causal_book_observation_v1",
                "paper_only":True,"authenticated_execution":False,
                "real_order_submission":False,
                "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
                "valid":True,"lineage_continuous":True,
                "model_sha":"a"*40,"receive_wall_ms":ms,
                "market_id":"m1","token_id":"y","connection_epoch":1,
                "observer_session_id":"s","observer_sequence":seq,
                "placement_features":{"imbalance":0.2},
                "bid_depth_l1":10.0,"ask_depth_l1":8.0,
            }
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            book=root/"research"/"repricing_book"/"book_observations"
            book.mkdir(parents=True)
            rows=[record(1100,1),record(1600,2),record(2100,3)]
            (book/"current.jsonl").write_text(
                "".join(json.dumps(r)+"\n" for r in rows),encoding="utf-8")
            anchors,diag=load_book_anchor_rows(
                root,minimum_wall_ns=1_000_000_000,metadata=metadata,
                market_meta={},context_meta={})
        self.assertEqual(
            [r["decision_ns"] for r in anchors],
            [1_500_000_000,2_000_000_000])
        self.assertEqual(
            [r["information_end_ns"] for r in anchors],
            [1_100_000_000,1_600_000_000])
        self.assertEqual(
            [r["signal_age_ns"] for r in anchors],
            [400_000_000,400_000_000])
        self.assertIn("EXACT_500MS_GRID",diag["timing_selection"])

    def test_book_anchor_reads_london_archive(self):
        import json,tempfile
        metadata={
            "m-arch":{
                "market_id":"m-arch","asset":"BTC","horizon":"M5",
                "yes_token":"y","no_token":"n",
                "start_timestamp_ms":1000,"end_timestamp_ms":4000,
                "fee_rate":0.07,"fee_exponent":1.0,"minimum":5.0,
            }
        }
        def record(ms,seq):
            return {
                "schema":"polymarket_v7_causal_book_observation_v1",
                "paper_only":True,"authenticated_execution":False,
                "real_order_submission":False,
                "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
                "valid":True,"lineage_continuous":True,
                "model_sha":"b"*40,"receive_wall_ms":ms,
                "market_id":"m-arch","token_id":"y","connection_epoch":1,
                "observer_session_id":"arch","observer_sequence":seq,
                "placement_features":{"imbalance":0.1},
                "bid_depth_l1":5.0,"ask_depth_l1":6.0,
            }
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"paper_v7_london"
            archive=root.parent/"paper_v7_london_archives"/"r1"/"research"/"repricing_book"/"book_observations"
            archive.mkdir(parents=True)
            (archive/"segment.jsonl").write_text(
                "".join(json.dumps(record(ms,i+1))+"\n" for i,ms in enumerate((1100,1600,2100,2600))),
                encoding="utf-8")
            anchors,diag=load_book_anchor_rows(
                root,minimum_wall_ns=1_000_000_000,metadata=metadata,
                market_meta={},context_meta={})
        self.assertGreaterEqual(len(anchors),3)
        self.assertEqual(diag["counts"]["files"],1)
        self.assertIn("EXACT_500MS_GRID",diag["timing_selection"])

    def test_recent_trade_flow_excludes_same_millisecond(self):
        row={
            "decision_ns":1_000_000_000,
            "market_id":"m1","yes_token_id":"y","no_token_id":"n",
        }
        trades={
            ("m1","y"):{
                "stamps":[999,1000],
                "rows":[
                    (999,1,"BUY",0.5,2.0),
                    (1000,1,"SELL",0.5,7.0),
                ],
            }
        }
        flow=recent_trade_flow(row,trades,window_ms=1000)
        self.assertEqual(flow["pm.yes_aggressive_buy_shares_1s"],2.0)
        self.assertEqual(flow["pm.yes_aggressive_sell_shares_1s"],0.0)

    def test_raw_session_fallback_uses_robust_gzip_reader(self):
        import gzip,json,tempfile
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            book=root/"research"/"repricing_book"/"book_observations"
            book.mkdir(parents=True)
            row={
                "schema":"polymarket_v7_causal_book_observation_v1",
                "observer_session_id":"s","connection_epoch":1,
                "observer_sequence":1,"receive_wall_ms":1000,
                "market_id":"m1","token_id":"y",
                "lineage_continuous":True,"valid":True,
                "best_bid":0.49,"best_ask":0.51,"tick_size":0.01,
                "bid_depth_l1":10.0,"ask_depth_l1":12.0,
            }
            with gzip.open(book/"segment.jsonl.gz","wt",encoding="utf-8") as handle:
                handle.write(json.dumps(row)+"\n")
            anchors=[{
                "market_id":"m1","yes_token_id":"y","no_token_id":"n",
                "token_id":"y","decision_ns":1_000_000_000,
            }]
            sessions,diag=stream_raw_sessions(root,anchors)
        self.assertEqual(diag["rows_seen"],1)
        self.assertEqual(diag["rows_retained"],1)
        self.assertEqual(len(sessions),1)

    def test_split_is_exact_time_60_40(self):
        start=1_000_000_000_000
        rows=[
            {"decision_ns":start+int(frac*WINDOW_NS)}
            for frac in (0.0,0.2,0.5999,0.60,0.8,0.999)
        ]
        train,oos,cut=split_60_40(rows,start)
        self.assertEqual(len(train),3)
        self.assertEqual(len(oos),3)
        self.assertEqual(cut,start+int(0.60*WINDOW_NS))


if __name__=="__main__":
    unittest.main()
