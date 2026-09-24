from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from research.walk_forward_v3.maker_a0_a5_2h import (
    Ridge, external_feature, pm_feature, full_execution_feature, feature_dict, split_60_40,
    WINDOW_NS,
)


class MakerA0A5Tests(unittest.TestCase):
    def test_feature_families_are_interpretable(self):
        self.assertTrue(external_feature("binance_return_100ms_bp"))
        self.assertTrue(external_feature("tape.external.dispersion_bps"))
        self.assertFalse(external_feature("tape.pm_yes_imbalance"))
        self.assertTrue(pm_feature("tape.pm_yes_imbalance"))
        self.assertTrue(pm_feature("short_return_ticks"))
        self.assertTrue(full_execution_feature("queue_ahead"))
        self.assertTrue(full_execution_feature("distance_to_reference_bp"))
        self.assertFalse(full_execution_feature("realized_markout_1s"))

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

    def test_ridge_fits_simple_relation(self):
        rows=[{"x":float(i)} for i in range(100)]
        y=[2.0*float(i)+1.0 for i in range(100)]
        model=Ridge(["x"],ridge=1e-6).fit(rows,y)
        self.assertAlmostEqual(model.predict({"x":12.0}),25.0,places=3)

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
