from pathlib import Path
import sys, unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"scripts"))
from v7_fair_model_artifact import FairModelArtifact, ArtifactError, canonical_hash

class ArtifactTests(unittest.TestCase):
    def model(self):
        return FairModelArtifact.build(family="x",model_version="m",feature_schema_version="f",
            code_sha="a"*40,policy_version="p",artifact_role="RESEARCH",training_start_ns=1,
            training_end_ns=2,training_contracts=1,training_days=1,assets=("BTC",),
            contract_templates=("T",),rules_hashes=("b"*64,),parameters={},hyperparameters={},
            oos_scores={},probability_interval_diagnostics={},economic_replay={},generated_timestamp_ns=3)
    def test_research_artifact_hash_is_stable_and_valid(self):
        m=self.model();m.validate();self.assertEqual(len(m.model_hash),64)
        self.assertEqual(canonical_hash({"b":2,"a":1}),canonical_hash({"a":1,"b":2}))
    def test_nonresearch_role_rejected(self):
        raw=self.model().__dict__.copy();raw["artifact_role"]="INVALID_ROLE"
        with self.assertRaises(ArtifactError):FairModelArtifact(**raw).validate()
if __name__=="__main__":unittest.main()
