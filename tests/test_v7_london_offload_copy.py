from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'research/pull_london_evidence.sh'
RSYNC = shutil.which('rsync')


def arguments(script, source, destination):
    command = script[script.index('\nrsync ') + 1:script.index('\nreceipt_tmp=')]
    args = shlex.split(command.replace('\\\n', ''))
    args[0] = RSYNC or 'rsync'
    args[-2:] = [str(source) + '/', str(destination) + '/']
    return args


class LondonOffloadCopyTests(unittest.TestCase):
    @unittest.skipUnless(RSYNC, 'rsync executable unavailable')
    def test_mutable_snapshots_and_control_identity_copy_with_installed_rsync(self):
        script = SCRIPT.read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'source'; destination = root / 'destination'
            (source / 'external_fair').mkdir(parents=True); (source / 'control').mkdir(); destination.mkdir()
            status = source / 'external_fair/status.json'; status.write_text('{"counter":1000}\n')
            identity = source / 'control/runtime_identity.json'; identity.write_text('{"run_id":"fixture"}\n')
            artifact = source / 'control/runtime_artifact_receipt.json'; artifact.write_text('{"sha":"fixture"}\n')
            (source / 'control/private-unselected.txt').write_text('must not be copied')
            args = arguments(script, source, destination)
            for index, text in enumerate(('{"counter":1000}\n', '{"counter":1001}\n', '{"counter":1}\n')):
                status.write_text(text); stamp = time.time() + 3 * index; os.utime(status, (stamp, stamp))
                result = subprocess.run(args, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((destination / 'external_fair/status.json').read_text(), text)
            self.assertEqual((destination / 'control/runtime_identity.json').read_bytes(), identity.read_bytes())
            self.assertEqual((destination / 'control/runtime_artifact_receipt.json').read_bytes(), artifact.read_bytes())
            self.assertFalse((destination / 'control/private-unselected.txt').exists())

    @unittest.skipUnless(RSYNC, 'rsync executable unavailable')
    def test_old_control_filter_loses_identity_even_without_append_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'source'; destination = root / 'destination'
            (source / 'control').mkdir(parents=True); destination.mkdir()
            (source / 'control/runtime_identity.json').write_text('{"run_id":"fixture"}\n')
            args = arguments(SCRIPT.read_text(), source, destination)
            args.remove('--include=/control/')
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((destination / 'control/runtime_identity.json').exists())

    def test_append_only_and_inplace_options_are_forbidden_for_mixed_snapshots(self):
        args = arguments(SCRIPT.read_text(), Path('/fixture/source'), Path('/fixture/destination'))
        self.assertNotIn('--append', args)
        self.assertNotIn('--append-verify', args)
        self.assertNotIn('--inplace', args)
        self.assertIn('--partial-dir=.rsync-partial', args)

    def test_offload_receipt_never_certifies_open_unsegmented_partial_or_symlinked_tapes(self):
        script = SCRIPT.read_text()
        code = script.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); current = root / 'current'; (root / 'receipts').mkdir()
            raw = current / 'external_fair/raw'; raw.mkdir(parents=True)
            closed = raw / 'source.segment-000001.bin'; closed.write_bytes(b'closed')
            active = raw / 'source.segment-000002.bin.open'; active.write_bytes(b'open')
            (raw / 'legacy.bin').write_bytes(b'still active')
            partial = raw / '.rsync-partial'; partial.mkdir(); (partial / 'x.segment-000001.bin').write_bytes(b'partial')
            (raw / 'link.segment-000001.bin').symlink_to(closed)
            result_path = root / 'receipt.json'
            result = subprocess.run(['python3', '-', str(root), 'fixture-host', str(result_path)],
                                    input=code, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result_path.read_text())
            self.assertEqual([f['path'] for f in receipt['files']], [str(closed.relative_to(current))])
            self.assertTrue(active.exists())
            self.assertFalse(receipt['zero_fill_missing'])


if __name__ == '__main__':
    unittest.main()
