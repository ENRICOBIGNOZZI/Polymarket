#!/usr/bin/env python3
"""Stage exact-SHA Linux monitoring assets; never mutate or start a service."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

SHA40 = re.compile(r'^[0-9a-f]{40}$')
SAFE_DEPLOY_PATH = re.compile(r'^/[A-Za-z0-9_./-]+$')


def source_file(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if not relative or rel.is_absolute() or '..' in rel.parts:
        raise ValueError('monitoring asset must be repository-relative')
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f'monitoring asset missing or outside release: {relative}')
    return path


def source_sha(root: Path) -> str:
    marker = root / 'deploy/london/runtime_sha'
    if marker.is_file():
        value = marker.read_text().strip()
    else:
        value = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                        text=True, stderr=subprocess.DEVNULL).strip()
        dirty = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=all'], text=True)
        if dirty.strip():
            raise ValueError('exact-SHA monitoring requires a clean source checkout')
    if not SHA40.fullmatch(value):
        raise ValueError('monitoring release has no exact SHA')
    return value


def render_once(text: str, marker: str, replacement: str) -> str:
    if text.count(marker) != 1:
        raise ValueError(f'expected exactly one monitoring marker: {marker}')
    value = text.replace(marker, replacement)
    if '__POLYMARKET_' in value:
        raise ValueError('unresolved monitoring template marker')
    return value


def build(root: Path, output: Path, deployed_directory: Path, expected_sha: str) -> dict:
    root = root.resolve()
    if not SHA40.fullmatch(expected_sha) or source_sha(root) != expected_sha:
        raise ValueError('monitoring source SHA mismatch')
    destination = str(deployed_directory)
    if not SAFE_DEPLOY_PATH.fullmatch(destination) or '..' in deployed_directory.parts:
        raise ValueError('absolute deployment directory without shell/systemd metacharacters required')
    manifest = json.loads(source_file(root, 'monitoring/v7_monitoring_manifest.json').read_text())
    if (manifest.get('schema') != 'polymarket_v7_monitoring_manifest_v3'
            or manifest.get('version') != 7
            or manifest.get('paper_only') is not True
            or manifest.get('authenticated_execution') is not False
            or manifest.get('real_order_submission') is not False):
        raise ValueError('invalid PAPER-only monitoring contract')
    grafana, prometheus = manifest['grafana'], manifest['prometheus']
    if grafana.get('datasource_uid') != 'prometheus-v7':
        raise ValueError('unexpected datasource identity')
    assets: dict[str, bytes] = {}
    dashboard_files = [grafana[key] for key in (
        'dashboard_file',
        'latency_dashboard',
        'external_fair_dashboard',
        'multi_crypto_dashboard',
        'pure_arb_dashboard',
    ) if grafana.get(key)]
    seen_uids: set[str] = set()
    for relative in dashboard_files:
        path = source_file(root, relative)
        data = path.read_bytes()
        dashboard = json.loads(data)
        uid = dashboard.get('uid')
        if not isinstance(uid, str) or not uid or uid in seen_uids:
            raise ValueError('missing or duplicate dashboard UID')
        if not isinstance(dashboard.get('panels'), list):
            raise ValueError('dashboard panels missing')
        seen_uids.add(uid)
        assets['grafana/dashboards/' + path.name] = data
    if grafana['dashboard_uid'] not in seen_uids:
        raise ValueError('canonical dashboard absent from monitoring bundle')
    datasource = source_file(root, grafana['datasource_file']).read_text()
    if not re.search(r'^\s*url:\s*http://127\.0\.0\.1:9090\s*$', datasource, re.MULTILINE):
        raise ValueError('Grafana datasource must use the local canonical Prometheus')
    assets['grafana/provisioning/datasources/prometheus-v7.yml'] = datasource.encode()
    provider = source_file(root, grafana['provider_file']).read_text()
    provider = render_once(provider, '__POLYMARKET_V7_DASHBOARD_DIR__',
                           destination + '/grafana/dashboards')
    assets['grafana/provisioning/dashboards/v7.yml'] = provider.encode()
    assets['prometheus-v7-alerts.yml'] = source_file(root, prometheus['alert_rules']).read_bytes()
    config = source_file(root, prometheus['config']).read_text()
    config = render_once(config, '__POLYMARKET_V7_ALERT_RULES__',
                         destination + '/prometheus-v7-alerts.yml')
    assets['prometheus-v7.yml'] = config.encode()
    # Preserve Grafana's existing authentication and storage settings. These
    # overrides only bind the listener and point provisioning at this release.
    home = destination + '/grafana/dashboards/' + Path(grafana['dashboard_file']).name
    override = ('[Service]\n'
                'Environment="GF_SERVER_HTTP_ADDR=127.0.0.1"\n'
                'Environment="GF_SERVER_HTTP_PORT=3000"\n'
                f'Environment="PROVISIONING_CFG_DIR={destination}/grafana/provisioning"\n'
                f'Environment="GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH={home}"\n')
    assets['grafana-systemd-override.conf'] = override.encode()
    prom_override = ('[Service]\n'
                     f'Environment="ARGS=--config.file={destination}/prometheus-v7.yml --storage.tsdb.path=/var/lib/prometheus/metrics2"\n')
    assets['prometheus-systemd-override.conf'] = prom_override.encode()
    receipt = {'schema': 'polymarket_v7_london_monitoring_bundle_v1',
               'runtime_sha': expected_sha, 'paper_only': True,
               'authenticated_execution': False, 'real_order_submission': False,
               'deployed_directory': destination,
               'dashboard_uids': sorted(seen_uids), 'services_started': False,
               'authentication_changed': False,
               'files': {path: hashlib.sha256(data).hexdigest() for path, data in sorted(assets.items())}}
    # An existing output is evidence, not scratch space. Never silently replace it.
    output.mkdir(parents=True, exist_ok=False)
    for relative, data in assets.items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (output / 'monitoring_bundle_receipt.json').write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--deployed-directory', type=Path, required=True)
    parser.add_argument('--expected-sha', required=True)
    args = parser.parse_args()
    try:
        receipt = build(args.repository_root, args.output, args.deployed_directory, args.expected_sha)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(2, f'monitoring bundle refused: {exc}\n')
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
