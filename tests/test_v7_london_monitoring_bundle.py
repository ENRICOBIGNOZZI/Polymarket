from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('london_monitoring_bundle', ROOT / 'ops/v7_london_monitoring_bundle.py')
BUNDLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUNDLE)
SHA = 'a' * 40


class LondonMonitoringBundleTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory) / 'source'
        (root / 'deploy/london').mkdir(parents=True)
        (root / 'deploy/london/runtime_sha').write_text(SHA + '\n')
        shutil.copytree(ROOT / 'monitoring/grafana', root / 'monitoring/grafana')
        for name in ('v7_monitoring_manifest.json', 'prometheus_v7.yml', 'v7_alerts.yml'):
            shutil.copy2(ROOT / 'monitoring' / name, root / 'monitoring' / name)
        return root

    def test_bundle_renders_final_paths_and_preserves_dashboard_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory); out = Path(directory) / 'out'
            final = Path('/var/lib/polymarket-v7-monitoring/by-sha') / SHA
            receipt = BUNDLE.build(root, out, final, SHA)
            self.assertFalse(receipt['services_started'])
            self.assertFalse(receipt['authentication_changed'])
            self.assertEqual(receipt['runtime_sha'], SHA)
            for relative, digest in receipt['files'].items():
                self.assertEqual(hashlib.sha256((out / relative).read_bytes()).hexdigest(), digest)
            self.assertIn(str(final / 'prometheus-v7-alerts.yml'), (out / 'prometheus-v7.yml').read_text())
            self.assertIn(str(final / 'grafana/dashboards'), (out / 'grafana/provisioning/dashboards/v7.yml').read_text())
            original = root / 'monitoring/grafana/dashboards/polymarket-v7.json'
            self.assertEqual(original.read_bytes(), (out / 'grafana/dashboards/polymarket-v7.json').read_bytes())
            override = (out / 'grafana-systemd-override.conf').read_text()
            self.assertIn('GF_SERVER_HTTP_ADDR=127.0.0.1', override)
            self.assertIn('PROVISIONING_CFG_DIR=', override)
            self.assertNotIn('GF_AUTH', override)
            self.assertNotIn('PASSWORD', override)
            prom_override = (out / 'prometheus-systemd-override.conf').read_text()
            self.assertIn('--config.file=', prom_override)
            self.assertIn(str(final / 'prometheus-v7.yml'), prom_override)
            for path in out.rglob('*.yml'):
                self.assertNotIn('__POLYMARKET_', path.read_text())

    def test_refuses_wrong_sha_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory); out = Path(directory) / 'out'
            with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
                BUNDLE.build(root, out, Path('/var/lib/monitoring'), 'b' * 40)
            self.assertFalse(out.exists())

    def test_refuses_existing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory); out = Path(directory) / 'out'; out.mkdir()
            sentinel = out / 'old-evidence'; sentinel.write_text('keep')
            with self.assertRaises(FileExistsError):
                BUNDLE.build(root, out, Path('/var/lib/monitoring'), SHA)
            self.assertEqual(sentinel.read_text(), 'keep')

    def test_refuses_unsafe_paths_or_remote_datasource(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory); out = Path(directory) / 'out'
            for destination in (Path('relative'), Path('/var/lib/x%h'), Path('/var/lib/x\nEnvironment=BAD')):
                with self.subTest(destination=destination), self.assertRaises(ValueError):
                    BUNDLE.build(root, out, destination, SHA)
            data = root / 'monitoring/grafana/provisioning/datasources/prometheus-v7.yml'
            data.write_text(data.read_text().replace('http://127.0.0.1:9090', 'http://example.invalid:9090'))
            with self.assertRaisesRegex(ValueError, 'local canonical Prometheus'):
                BUNDLE.build(root, out, Path('/var/lib/monitoring'), SHA)
            self.assertFalse(out.exists())

    def test_all_assets_are_shipped_in_london_runtime(self):
        manifest = json.loads((ROOT / 'deploy/london/runtime_manifest.json').read_text())
        assets = set(manifest['support_files'])
        for relative in ('ops/v7_london_monitoring_bundle.py', 'ops/v7_london_install_monitoring.sh', 'monitoring/prometheus_v7.yml',
                         'monitoring/grafana/dashboards/polymarket-v7.json',
                         'monitoring/grafana/provisioning/datasources/prometheus-v7.yml',
                         'monitoring/grafana/provisioning/dashboards/v7.yml'):
            self.assertIn(relative, assets)

    def test_clean_git_source_has_exact_sha_and_dirty_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'file.txt').write_text('clean')
            subprocess.run(['git', '-C', str(root), 'add', 'file.txt'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'fixture'], check=True)
            sha = BUNDLE.source_sha(root)
            self.assertRegex(sha, r'^[0-9a-f]{40}$')
            (root / 'file.txt').write_text('dirty')
            with self.assertRaisesRegex(ValueError, 'clean source checkout'):
                BUNDLE.source_sha(root)


if __name__ == '__main__':
    unittest.main()
