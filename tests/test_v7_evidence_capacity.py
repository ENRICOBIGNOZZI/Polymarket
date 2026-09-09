import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from v7_evidence_capacity import allocated_data_bytes,capacity
from v7_storage_budget import MAX_MANAGED_DATA_BYTES,RETENTION_TRIGGER_BYTES,RETENTION_TARGET_BYTES


class CapacityTests(unittest.TestCase):
    def test_hardlinks_and_overlapping_roots_are_counted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sub=root/'sub';sub.mkdir();a=root/'a';a.write_bytes(bytes(100000));os.link(a,sub/'alias')
            expected=root.stat().st_blocks*512+sub.stat().st_blocks*512+a.stat().st_blocks*512
            self.assertEqual(allocated_data_bytes([root,sub]),expected)
            (sub/'symlink').symlink_to(a)
            expected=root.stat().st_blocks*512+sub.stat().st_blocks*512+a.stat().st_blocks*512
            self.assertEqual(allocated_data_bytes([root]),expected)
            with self.assertRaises(ValueError):allocated_data_bytes([root/'missing'])

    def test_cap_alert_does_not_require_a_throughput_estimate(self):
        r=capacity([],100_000_000_000,200_000_000_000,data_bytes=41_000_000_000)
        self.assertEqual(r['state'],'DATA_BUDGET_COMPACTION_REQUIRED')
        self.assertEqual(r['minimum_reduction_bytes_to_current_cap'],1_000_000_000)
        self.assertIsNone(r['rate_estimate'])

    def test_40gb_budget_and_38gb_retention_trigger(self):
        below=capacity([],100_000_000_000,200_000_000_000,data_bytes=37_999_999_999)
        self.assertEqual(below['budget_state'],'WITHIN_CAP')
        self.assertNotEqual(below['state'],'DATA_BUDGET_COMPACTION_REQUIRED')
        at=capacity([],100_000_000_000,200_000_000_000,data_bytes=38_000_000_000)
        self.assertEqual(at['state'],'DATA_BUDGET_COMPACTION_REQUIRED')
        self.assertEqual(at['maximum_total_data_bytes'],40_000_000_000)
        cfg=json.loads((Path(__file__).resolve().parents[1]/'config/v7_data_retention.json').read_text())
        self.assertEqual(cfg['permanent_evidence']['maximum_total_data_bytes'],MAX_MANAGED_DATA_BYTES)
        self.assertEqual(cfg['aggregate_retention']['trigger_bytes'],RETENTION_TRIGGER_BYTES)
        self.assertEqual(cfg['aggregate_retention']['target_bytes'],RETENTION_TARGET_BYTES)

    def test_initial_backlog_and_repeated_hashes_are_not_new_production(self):
        def row(stamp,sha):return {'timestamp':stamp,'source_sha256':sha,'source_bytes':1000,'gzip_bytes':100,
            'scope':'ACTIVE_RUN_CLOSED_SEGMENT','decompressed_sha256_verified':True}
        result=capacity([row(100,'A'),row(200,'A'),row(200,'B'),row(300,'B'),row(300,'C')],
                        100_000_000_000,200_000_000_000,data_bytes=1)
        rate=result['rate_estimate']
        self.assertEqual(rate['source_sha256s'],['B','C'])
        self.assertEqual(rate['raw_bytes'],2000)
        self.assertEqual(rate['measurement_seconds'],200)


if __name__=='__main__':unittest.main()
