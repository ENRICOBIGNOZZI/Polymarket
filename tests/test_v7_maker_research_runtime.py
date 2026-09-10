from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]

class MakerResearchRuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text=(ROOT/"scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    def test_one_current_run_research_model_only(self):
        self.assertIn('MAKER_RESEARCH_MODEL="$RUN_ROOT/micro_maker/execution_model.json"',self.text)
        self.assertIn('export PM_V7_MAKER_EXECUTION_MODEL="$MAKER_RESEARCH_MODEL"',self.text)
        self.assertIn('--output-model "$MAKER_RESEARCH_MODEL"',self.text)
        self.assertIn('--model "$MAKER_RESEARCH_MODEL"',self.text)
        self.assertNotIn('MAKER_MODEL_REGISTRY',self.text)
    def test_research_evidence_resets_at_run_start(self):
        self.assertIn('MAKER_RESEARCH_STORE="$DURABLE_ROOT/micro_maker/research_evidence.jsonl"',self.text)
        self.assertNotIn(': > "$MAKER_RESEARCH_STORE"',self.text)
        self.assertNotIn('--source-root runs/paper_v7_archives',self.text)

if __name__=="__main__":unittest.main()
