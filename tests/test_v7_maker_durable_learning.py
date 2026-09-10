import gzip
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_maker_durable_learning import (  # noqa: E402
    adverse_markout_models, append_new, compact_evidence, fit_model, hazard_model, identity,
    evidence_files, compatible_archive_file, placement_features, rows, research_policy_value, materialize_research_model,
    placement_action, exact_execution_cell, order_examples,
)

SHA = "a" * 40


def record(event_type: str, record_id: str, **extra):
    value = {
        "event_type": event_type, "record_id": record_id,
        "strategy": "CRYPTO_SETTLEMENT_ENGINE", "model_sha": SHA,
        "paper_only": True, "authenticated_execution": False,
        "recorded_ts_ms": 1_000,
        "metadata": {
            "component": "professional_maker", "model_family": "professional_maker",
            "policy_hash": "policy", "config_hash": "config",
            "execution_semantics_version": "maker-paper-v7.2-bilateral-inventory",
            "outcome": "YES", "action": "JOIN", "execution_side": "BUY",
        },
    }
    value.update(extra)
    return value


class DurableLearningTests(unittest.TestCase):
    def test_make_placement_and_legacy_terminal_classification(self):
        order=record('ORDER_SUBMITTED','submit',order_id='o',market_id='m',token_id='t',
                     side='BUY',intended_action='MAKE',intended_size=2.)
        order['metadata']['opportunity_envelope']={'reasons':['PLACEMENT_JOIN']}
        self.assertEqual(placement_action(order),'JOIN')
        self.assertEqual(exact_execution_cell(order),('m','t','JOIN','BUY'))
        order['metadata']['placement_action']='IMPROVE1'
        self.assertEqual(placement_action(order),'IMPROVE1')
        terminal=record('ORDER_STATE','cancel',order_id='o',recorded_ts_ms=2000,order_state='CANCELLED')
        row=order_examples([order,terminal])[0]
        self.assertEqual(row['execution_outcome'],'TERMINAL_UNCLASSIFIED')
        self.assertIsNotNone(row['placement_exclusion_reason'])
        terminal['metadata']['execution_outcome']=4
        self.assertEqual(order_examples([order,terminal])[0]['execution_outcome'],'PRICE_NOT_REACHED')

    def test_same_order_id_across_generations_never_cross_joins(self) -> None:
        old=record("ORDER_SUBMITTED","old-order",order_id="shared",intended_size=5.0,
                   recorded_ts_ms=1_000)
        old["model_sha"]="b"*40
        old_terminal=record("ORDER_STATE","old-terminal",order_id="shared",
                            order_state="CANCELLED",recorded_ts_ms=2_000)
        old_terminal["model_sha"]="b"*40
        old_terminal["metadata"]["execution_outcome"]="NO_OPPOSITE_FLOW"
        new=record("ORDER_SUBMITTED","new-order",order_id="shared",intended_size=5.0,
                   recorded_ts_ms=10_000)
        new["model_sha"]="c"*40
        new_fill=record("FILL","new-fill",order_id="shared",filled_size=5.0,
                        recorded_ts_ms=10_500)
        new_fill["model_sha"]="c"*40
        new_terminal=record("ORDER_STATE","new-terminal",order_id="shared",
                            order_state="FILLED",recorded_ts_ms=10_600)
        new_terminal["model_sha"]="c"*40
        new_terminal["metadata"]["execution_outcome"]="FILLED"
        examples=order_examples([old,old_terminal,new,new_fill,new_terminal])
        self.assertEqual(len(examples),2)
        by_sha={row["source_model_sha"]:row for row in examples}
        self.assertEqual(by_sha["b"*40]["filled_fraction"],0.0)
        self.assertEqual(by_sha["b"*40]["execution_outcome"],"NO_OPPOSITE_FLOW")
        self.assertEqual(by_sha["c"*40]["filled_fraction"],1.0)
        self.assertEqual(by_sha["c"*40]["execution_outcome"],"FILLED")

    def test_research_model_is_atomically_refit_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path=pathlib.Path(folder)/"execution_model.json"
            model=fit_model([],model_sha=SHA,policy_hash="p",config_hash="c",cold_fill_prior=.02)
            first=materialize_research_model(path,model)
            self.assertEqual(first["artifact_role"],"research")
            self.assertTrue(first["research_runtime_model"])
            changed=dict(model);changed["generated_ts_ms"]+=300_000;changed["model_state"]="EVIDENCE_ACCUMULATING"
            second=materialize_research_model(path,changed)
            self.assertEqual(json.loads(path.read_text())["generated_ts_ms"],second["generated_ts_ms"])
            self.assertNotEqual(first["generated_ts_ms"],second["generated_ts_ms"])
            unsafe=dict(model);unsafe["real_order_submission"]=True
            with self.assertRaisesRegex(ValueError,"safety_contract"):
                materialize_research_model(path,unsafe)

    def test_research_markout_files_are_discovered_without_scanning_other_json(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            evidence = root / "research/evidence/maker_markout/mark.json"
            evidence.parent.mkdir(parents=True)
            expected = record(
                "MARKOUT", "research-mark", order_id="o1", fill_id="f1",
                markouts={"45s": -0.01},
            )
            evidence.write_text(json.dumps(expected) + "\n", encoding="utf-8")
            unrelated = root / "config.json"
            unrelated.write_text("{}\n", encoding="utf-8")
            self.assertEqual(evidence_files([root]), [evidence.resolve()])
            self.assertEqual(list(rows([evidence]))[0]["record_id"], "research-mark")

    def test_archive_identity_filter_is_fail_closed_and_cached(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder)
            matching=root/("cutover-"+"b"*40+"-1-2")
            other=root/("cutover-"+"c"*40+"-1-3")
            for archive,policy in ((matching,"policy"),(other,"other")):
                (archive/"micro_maker").mkdir(parents=True)
                (archive/"ledger").mkdir()
                (archive/"micro_maker/execution_model.json").write_text(json.dumps({
                    "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
                    "policy_hash":policy,"config_hash":"config",
                    "execution_semantics_version":"maker-paper-v7.2-bilateral-inventory"}))
            cache={}
            self.assertTrue(compatible_archive_file(matching/"ledger/execution.jsonl.gz","policy","config",cache))
            self.assertFalse(compatible_archive_file(other/"ledger/execution.jsonl.gz","policy","config",cache))
            self.assertTrue(compatible_archive_file(root/"live/execution.jsonl","policy","config",cache))
            missing=root/("cutover-"+"d"*40+"-1-4")/"ledger/execution.jsonl.gz"
            self.assertFalse(compatible_archive_file(missing,"policy","config",cache))

    def test_jsonl_gzip_evidence_is_discovered_and_read_losslessly(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            evidence = root / "archive/ledger/execution.jsonl.gz"
            evidence.parent.mkdir(parents=True)
            expected = record("ORDER_SUBMITTED", "gz-order", order_id="o-gz", intended_size=5.0)
            payload = (json.dumps(expected) + "\n").encode()
            evidence.write_bytes(gzip.compress(payload, mtime=0))
            self.assertEqual(evidence_files([root]), [evidence.resolve()])
            loaded = list(rows([evidence]))
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["record_id"], "gz-order")

    def test_compaction_keeps_only_current_run_exact_policy_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            source = root / "execution.jsonl"
            store = root / "evidence.jsonl"
            current_order = record("ORDER_SUBMITTED", "current-order", order_id="current", intended_size=5.0)
            current_state = record("ORDER_STATE", "current-state", order_id="current", order_state="CANCELLED")
            current_fill = record("FILL", "current-fill", order_id="current", filled_size=1.0)
            current_fill["metadata"] = {"component": "professional_maker"}
            old_order = record("ORDER_SUBMITTED", "old-order", order_id="old", intended_size=5.0)
            old_order["metadata"]["policy_hash"] = "old-policy"
            candidate = record("CANDIDATE", "candidate")
            source.write_text("".join(json.dumps(row)+"\n" for row in (current_order,current_state,current_fill,old_order,candidate)), encoding="utf-8")
            values,status=compact_evidence([source],store_path=store,policy_hash="policy",config_hash="config")
            self.assertEqual({row["record_id"] for row in values},{"current-order","current-state","current-fill"})
            self.assertEqual(status["exact_policy_orders"],1)
            self.assertEqual(status["evidence_scope"],"CROSS_CUTOVER_EXACT_POLICY_CONFIG")
            self.assertEqual(status["retained_records"],3)

    def test_append_store_deduplicates_within_current_run(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = pathlib.Path(folder) / "evidence.jsonl"
            row = record("ORDER_SUBMITTED", "r1", order_id="o1", intended_size=5.0)
            self.assertEqual(append_new(store, [row, row]), (1, 1))
            self.assertEqual(append_new(store, [row]), (0, 1))
            self.assertEqual(len(store.read_text().splitlines()), 1)
            self.assertEqual(identity(row)[-1], "r1")

    def test_cancel_is_right_censored_not_identical_bernoulli(self) -> None:
        sample = [
            {"exposure_ms": 60_000, "first_fill_ms": 30_000,
             "filled_fraction": 0.5, "censored": False, "event_cluster": "a"},
            {"exposure_ms": 2_000, "first_fill_ms": None,
             "filled_fraction": 0.0, "censored": True, "event_cluster": "b"},
        ]
        model = hazard_model(sample, 0.02)
        self.assertEqual(model["censored_orders"], 1)
        self.assertGreater(model["p_any_fill_by_seconds"]["30"], 0.0)
        self.assertEqual(model["censoring_semantics"], "cancel_or_observation_end_is_right_censored")

    def test_sparse_zero_fills_shrink_without_absorbing_exploration(self) -> None:
        sample = [
            {"exposure_ms": 60_000, "first_fill_ms": None,
             "filled_fraction": 0.0, "censored": True,
             "event_cluster": f"event-{index}"}
            for index in range(8)
        ]
        model = hazard_model(sample, 0.02, 20.0)
        self.assertEqual(model["raw_expected_filled_fraction_60s"], 0.0)
        self.assertAlmostEqual(model["expected_filled_fraction_60s"], 0.4 / 28.0)
        self.assertGreater(model["expected_filled_fraction_60s"], 0.0)
        self.assertLess(model["expected_filled_fraction_60s"], 0.02)
        self.assertEqual(model["fill_prior_strength_orders"], 20.0)

        values = []
        for index in range(8):
            order_id = f"zero-fill-{index}"
            values.append(record(
                "ORDER_SUBMITTED", f"submitted-{index}", order_id=order_id,
                event_id=f"event-{index}", intended_size=2.0,
                recorded_ts_ms=1_000 + index * 1_000,
            ))
            values.append(record(
                "ORDER_CANCELLED", f"cancelled-{index}", order_id=order_id,
                event_id=f"event-{index}", order_state="CANCELLED",
                recorded_ts_ms=1_500 + index * 1_000,
            ))
        fitted = fit_model(
            values, model_sha=SHA, policy_hash="policy", config_hash="config",
            cold_fill_prior=0.02, fill_prior_strength_orders=20.0,
        )
        self.assertEqual(fitted["family"], "censored_survival_hazard_joint_cycle_v4")
        self.assertAlmostEqual(
            fitted["groups"]["GLOBAL"]["fill_probability"], 0.4 / 28.0)
        self.assertAlmostEqual(
            fitted["groups"]["GLOBAL"]["empirical_fill_probability"], 0.4 / 28.0)
        self.assertEqual(
            fitted["groups"]["GLOBAL"]["fill_probability_semantics"],
            "beta_shrunk_censored_expected_filled_fraction_per_posted_share",
        )
        self.assertEqual(fitted["hyperparameters"]["fill_prior_strength_orders"], 20.0)
        self.assertFalse(fitted["groups"]["GLOBAL"]["mature"])
        self.assertEqual(fitted["groups"]["GLOBAL"]["filled_orders"], 0)

    def test_joint_model_is_direct_and_cold_start_is_explicit(self) -> None:
        model = fit_model([], model_sha=SHA, policy_hash="policy",
                          config_hash="config", cold_fill_prior=0.02)
        self.assertEqual(model["model_state"], "COLD_START")
        self.assertEqual(model["artifact_role"], "research")
        self.assertTrue(model["research_runtime_model"])
        self.assertFalse(model["joint_cycle_model"]["uses_product_of_marginals"])
        self.assertEqual(model["groups"]["GLOBAL"]["fill_probability"], 0.02)
        self.assertEqual(
            model["groups"]["GLOBAL"]["fill_probability_semantics"],
            "explicit_cold_start_prior",
        )
        self.assertFalse(model["learned_placement_policy"]["valid"])
        self.assertEqual(
            model["learned_placement_policy"]["state"], "EVIDENCE_ACCUMULATING")
        self.assertFalse(model["research_policy_value"]["research_value_supported"])

    def test_research_policy_value_reports_strict_economic_support_separately(self) -> None:
        values = []
        for index in range(20):
            candidate_id = f"probe-{index}"
            arm = "ECONOMIC" if index % 2 == 0 else "INFORMATION"
            timestamp = index * 86_400_000 + 1_000
            common_metadata = {
                "policy_hash": "policy", "config_hash": "config",
                "execution_semantics_version": "maker-paper-v7.2-bilateral-inventory",
                "counterfactual": True,
                "economic_authority": "SHADOW_COUNTERFACTUAL",
                "execution_authority": "SHADOW_ZERO_AUTHORITY",
                "excluded_from_portfolio_equity": True,
                "assigned_action_arm": arm, "action_propensity": 0.5,
            }
            assignment = record(
                "SHADOW_PROBE", f"probe-a-{index}", candidate_id=candidate_id,
                event_id=f"event-{index}", recorded_ts_ms=timestamp,
            )
            assignment["metadata"] = common_metadata | {
                "probe_phase": "ASSIGNED", "terminal": False,
            }
            terminal = record(
                "SHADOW_PROBE", f"probe-t-{index}", candidate_id=candidate_id,
                event_id=f"event-{index}", recorded_ts_ms=timestamp + 1_000,
            )
            terminal["metadata"] = common_metadata | {
                "probe_phase": "TERMINAL_REJECTED", "terminal": True,
            }
            values.extend((assignment, terminal))
        report = research_policy_value(values)
        self.assertEqual(report["terminal_episodes"], 20)
        self.assertEqual(report["forward_oos_episodes"], 4)
        self.assertEqual(report["invalid_records"], 0)
        self.assertFalse(report["research_value_supported"])
        self.assertIn(
            "MINIMUM_10000_FORWARD_OOS_QUOTE_EPISODES",
            report["blocking_reasons"],
        )
        self.assertEqual(report["arms"]["ECONOMIC"]["day_block_lcb95"], 0.0)

    def test_terminal_execution_funnel_labels_no_fill_stage(self) -> None:
        submitted = record(
            "ORDER_SUBMITTED", "submitted", order_id="o1", event_id="event-1",
            intended_size=5.0, recorded_ts_ms=1_000,
        )
        terminal = record(
            "ORDER_STATE", "terminal", order_id="o1", event_id="event-1",
            order_state="CANCELLED", recorded_ts_ms=6_000,
        )
        terminal["metadata"].update({
            "execution_outcome": "PRICE_NOT_REACHED",
            "opposite_flow_prints_seen": 3,
            "price_reach_prints_seen": 0,
            "opposite_flow_shares_seen": 12.5,
            "price_reach_shares_seen": 0.0,
        })
        fitted = fit_model(
            [submitted, terminal], model_sha=SHA, policy_hash="policy",
            config_hash="config", cold_fill_prior=0.02,
        )
        funnel = fitted["execution_funnel_labels"]
        self.assertEqual(funnel["terminal_orders"], 1)
        self.assertEqual(funnel["outcome_counts"]["PRICE_NOT_REACHED"], 1)
        self.assertEqual(funnel["opposite_flow_reach_rate"], 1.0)
        self.assertEqual(funnel["price_reach_rate"], 0.0)
        self.assertIsNone(funnel["queue_depletion_rate_given_price_reach"])

    def test_terminal_funnel_memory_is_scoped_to_exact_execution_cell(self) -> None:
        first = record(
            "ORDER_SUBMITTED", "submitted-1", order_id="o1", event_id="event-1",
            market_id="market-1", token_id="yes-1", intended_action="JOIN",
            side="BUY", intended_size=5.0, queue_ahead=100.0,
            recorded_ts_ms=1_000,
        )
        first_terminal = record(
            "ORDER_STATE", "terminal-1", order_id="o1", event_id="event-1",
            order_state="CANCELLED", recorded_ts_ms=6_000,
        )
        first_terminal["metadata"].update({
            "execution_outcome": "NO_OPPOSITE_FLOW",
            "opposite_flow_prints_seen": 0,
            "price_reach_prints_seen": 0,
        })
        second = record(
            "ORDER_SUBMITTED", "submitted-2", order_id="o2", event_id="event-2",
            market_id="market-2", token_id="yes-2", intended_action="JOIN",
            side="BUY", intended_size=5.0, queue_ahead=20.0,
            recorded_ts_ms=2_000,
        )
        second_terminal = record(
            "ORDER_STATE", "terminal-2", order_id="o2", event_id="event-2",
            order_state="CANCELLED", recorded_ts_ms=7_000,
        )
        second_terminal["metadata"].update({
            "execution_outcome": "QUEUE_NOT_DEPLETED",
            "opposite_flow_prints_seen": 2,
            "price_reach_prints_seen": 1,
        })
        stale = record(
            "ORDER_SUBMITTED", "submitted-stale", order_id="old",
            market_id="market-1", token_id="yes-1", intended_action="JOIN",
            side="BUY", intended_size=5.0, recorded_ts_ms=500,
        )
        stale["metadata"]["policy_hash"] = "old-policy"
        stale_terminal = record(
            "ORDER_STATE", "terminal-stale", order_id="old",
            order_state="CANCELLED", recorded_ts_ms=800,
        )
        stale_terminal["metadata"].update({
            "execution_outcome": "NO_OPPOSITE_FLOW",
            "policy_hash": "old-policy",
        })

        fitted = fit_model(
            [first, first_terminal, second, second_terminal, stale, stale_terminal],
            model_sha=SHA, policy_hash="policy", config_hash="config",
            cold_fill_prior=0.02,
        )
        exact = fitted["exact_execution_cells"]
        self.assertEqual(
            exact["identity"],
            ["market_id", "token_id", "action", "quote_side"],
        )
        self.assertEqual(
            exact["semantics"],
            "exact_cell_terminal_funnel_current_policy_only_v1",
        )
        cells = {row["market_id"]: row for row in exact["cells"]}
        self.assertEqual(set(cells), {"market-1", "market-2"})
        self.assertEqual(cells["market-1"]["terminal_orders"], 1)
        self.assertEqual(cells["market-1"]["no_opposite_flow"], 1)
        self.assertEqual(cells["market-2"]["queue_not_depleted"], 1)
        self.assertEqual(cells["market-2"]["price_reach_rate"], 1.0)
        self.assertEqual(
            cells["market-1"]["role"],
            "SELECTOR_FEEDBACK_ONLY_NO_EXECUTION_AUTHORITY",
        )

    def test_placement_features_are_side_oriented_and_complete(self) -> None:
        row = record("ORDER_SUBMITTED", "r", side="SELL")
        row["metadata"]["placement_features"] = {
            "spread_ticks": 2.0,
            "imbalance": 0.3,
            "ofi": -0.2,
            "ew_vol_ticks": 0.4,
            "trade_intensity": 0.5,
            "cancel_intensity": 0.6,
            "short_return_ticks": 0.7,
            "inventory_fraction": -0.8,
            "local_latency_ms": 0.9,
            "aggressive_buy_prints_per_second": 0.25,
            "aggressive_sell_prints_per_second": 0.50,
        }
        features = placement_features(row)
        self.assertIsNotNone(features)
        assert features is not None
        self.assertEqual(len(features), 13)
        self.assertEqual(features[:4], [1.0, 2.0, -0.3, 0.2])
        self.assertEqual(features[7], -0.7)
        self.assertEqual(features[8], 0.8)
        self.assertEqual(features[10], 0.25)
        self.assertEqual(features[11], 0.0)
        self.assertEqual(features[12], 0.0)

    def test_in_sample_size_never_claims_mature_without_oos(self) -> None:
        values = []
        for index in range(60):
            order_id = f"o{index}"
            values.append(record(
                "ORDER_SUBMITTED", f"r{index}", order_id=order_id,
                event_id=f"event-{index % 15}", intended_size=5.0,
                recorded_ts_ms=1_000 + index * 100,
            ))
        model = fit_model(values, model_sha=SHA, policy_hash="policy",
                          config_hash="config", cold_fill_prior=0.02)
        self.assertEqual(model["model_state"], "EVIDENCE_ACCUMULATING")
        self.assertEqual(model["artifact_role"], "research")
        self.assertTrue(model["research_runtime_model"])
        self.assertIsNone(model["validation_window"])
        self.assertIn(model["economic_evidence_state"], {"SUPPORTED", "DIAGNOSTIC_ACCUMULATING"})

    def test_fill_conditioned_markout_raises_current_run_risk_floor(self) -> None:
        order = record(
            "ORDER_SUBMITTED", "order", order_id="o1", intended_size=5.0,
            intended_action="IMPROVE1", side="SELL",
        )
        fill = record("FILL", "fill", order_id="o1", filled_size=5.0)
        mark_1 = record("MARKOUT", "mark-1", order_id="o1", markouts={"1s": 0.05})
        mark_45 = record("MARKOUT", "mark-45", order_id="o1", markouts={"45s": -0.10})
        risk = adverse_markout_models([order, fill, mark_1, mark_45])
        expected = (0.002 * 20.0 + 0.10 * 5.0) / 25.0
        self.assertAlmostEqual(risk["GLOBAL"]["adverse_markout_per_share"], expected)
        self.assertEqual(risk["GLOBAL"]["adverse_markout_observations"], 1)
        self.assertEqual(risk["GLOBAL"]["adverse_markout_event_clusters"], 1)
        self.assertEqual(risk["GLOBAL"]["adverse_markout_horizon_priority"][0], "45s")

        model = fit_model(
            [order, fill, mark_1, mark_45], model_sha=SHA,
            policy_hash="policy", config_hash="config", cold_fill_prior=0.02,
        )
        self.assertGreater(model["groups"]["GLOBAL"]["adverse_markout_per_share"], 0.002)
        self.assertEqual(model["groups"]["GLOBAL"]["adverse_markout_observations"], 1)
        self.assertEqual(model["compatible_policy_global_adverse"]["adverse_markout_observations"], 1)


    def test_symmetric_outcome_markout_pools_action_side_without_fill_credit(self) -> None:
        order = record(
            "ORDER_SUBMITTED", "order", order_id="o1", intended_size=5.0,
            intended_action="JOIN", side="SELL",
        )
        order["metadata"]["outcome"] = "YES"
        fill = record("FILL", "fill", order_id="o1", filled_size=5.0)
        mark = record("MARKOUT", "mark", order_id="o1", markouts={"45s": -0.05})

        model = fit_model(
            [order, fill, mark], model_sha=SHA, policy_hash="policy",
            config_hash="config", cold_fill_prior=0.02,
        )
        expected = (0.002 * 20.0 + 0.05 * 5.0) / 25.0
        pooled = model["risk_only_symmetric_outcome_adverse"]["JOIN|SELL"]
        self.assertAlmostEqual(pooled["adverse_markout_per_share"], expected)
        self.assertEqual(pooled["adverse_markout_observations"], 1)
        self.assertIn("JOIN|NO|SELL", model["groups"])
        self.assertEqual(model["groups"]["JOIN|NO|SELL"]["orders"], 0)
        self.assertEqual(model["groups"]["JOIN|NO|SELL"]["filled_orders"], 0)

    def test_placement_markout_inherits_identity_from_current_submitted_order(self) -> None:
        order = record(
            "ORDER_SUBMITTED", "order", order_id="o1", event_id="event-1",
            intended_size=5.0, intended_action="JOIN", side="SELL",
        )
        order["metadata"]["placement_features"] = {
            "spread_ticks": 2.0,
            "imbalance": 0.3,
            "ofi": -0.2,
            "ew_vol_ticks": 0.4,
            "trade_intensity": 0.5,
            "cancel_intensity": 0.6,
            "short_return_ticks": 0.7,
            "inventory_fraction": -0.8,
            "local_latency_ms": 0.9,
            "aggressive_buy_prints_per_second": 0.25,
            "aggressive_sell_prints_per_second": 0.50,
        }
        fill = record("FILL", "fill", order_id="o1", filled_size=5.0)
        markout = record(
            "MARKOUT", "markout", order_id="o1", markouts={"45s": -0.10})
        # Runtime lifecycle rows may omit the policy/config metadata.  Their
        # identity is inherited exclusively through the submitted order id.
        fill["metadata"] = {}
        markout["metadata"] = {}

        model = fit_model(
            [order, fill, markout], model_sha=SHA, policy_hash="policy",
            config_hash="config", cold_fill_prior=0.02,
        )

        self.assertEqual(model["groups"]["GLOBAL"]["filled_orders"], 1)
        self.assertEqual(model["learned_placement_policy"]["markout_examples"], 1)
        self.assertEqual(
            model["learned_placement_policy"]["state"], "EVIDENCE_ACCUMULATING")


if __name__ == "__main__":
    unittest.main()
