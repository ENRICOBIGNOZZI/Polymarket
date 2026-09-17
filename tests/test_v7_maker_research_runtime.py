from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]

class MakerResearchRuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime=(ROOT/"scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")
        cls.research=(ROOT/"research/build_runtime_artifacts.sh").read_text(encoding="utf-8")
    def test_london_consumes_one_frozen_model_only(self):
        self.assertIn('MAKER_RESEARCH_MODEL="$RUN_ROOT/micro_maker/execution_model.json"',self.runtime)
        self.assertIn('export PM_V7_MAKER_EXECUTION_MODEL="$MAKER_RESEARCH_MODEL"',self.runtime)
        self.assertIn('v7_runtime_artifacts.py',self.runtime)
        self.assertNotIn('--output-model "$MAKER_RESEARCH_MODEL"',self.runtime)
        self.assertIn('--model "$MAKER_RESEARCH_MODEL"',self.runtime)
        self.assertNotIn('v7_maker_durable_learning.py',self.runtime)
        self.assertNotIn('v7_external_rich_train.py',self.runtime)
    def test_training_and_durable_evidence_live_on_research_plane(self):
        self.assertIn('v7_maker_durable_learning.py',self.research)
        self.assertIn('research_evidence.jsonl',self.research)
        self.assertIn('v7_external_rich_train.py',self.research)
        self.assertNotIn(': > "$MAKER_RESEARCH_STORE"',self.runtime)

if __name__=="__main__":unittest.main()
