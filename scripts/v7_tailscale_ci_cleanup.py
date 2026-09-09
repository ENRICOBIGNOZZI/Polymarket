#!/usr/bin/env python3
"""Remove only explicitly scoped, expired, inactive GitHub runner nodes.

Requires a Tailscale API access token (not a node enrollment auth key).
The default is a read-only plan. No credentials are written to the audit log.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

RUNNER = re.compile(r'(?:github-runnervm[a-z0-9]+|gh-(?:health|universe|deploy)-[0-9]+-[0-9]+)\Z')


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else float('inf')
    except (TypeError, ValueError, AttributeError):
        return float('inf')


def exclusion(candidate, device, peers, self_id, now):
    node_id = candidate.get('ID')
    peer = peers.get(node_id, {})
    hostname = candidate.get('HostName', '')
    if not node_id or node_id == self_id:
        return 'SELF_OR_MISSING_ID'
    if not RUNNER.fullmatch(hostname):
        return 'UNRECOGNIZED_RUNNER_NAME'
    if device.get('nodeId') != node_id or device.get('hostname') != hostname:
        return 'ADMIN_IDENTITY_MISMATCH'
    if peer.get('HostName') != hostname or peer.get('Online') is not False or peer.get('Expired') is not True:
        return 'CURRENT_LOCAL_STATE_NOT_EXPIRED_OFFLINE'
    if device.get('keyExpiryDisabled') is not False or timestamp(device.get('expires')) >= now:
        return 'ADMIN_EXPIRY_NOT_CONFIRMED'
    if timestamp(device.get('lastSeen')) > now - 86400:
        return 'RECENT_OR_UNKNOWN_ACTIVITY'
    if set(device.get('tags', [])) - {'tag:ci'}:
        return 'NON_CI_TAGS'
    if device.get('isExternal') is not False:
        return 'EXTERNAL_OR_UNKNOWN_OWNERSHIP'
    return None


class API:
    def __init__(self, token):
        self.token = token

    def call(self, method, path):
        request = urllib.request.Request('https://api.tailscale.com/api/v2/' + path,
                                        method=method, headers={'Authorization': 'Bearer ' + self.token})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            # Do not echo server payloads or credentials on errors.
            raise RuntimeError(f'Tailscale {method} failed: HTTP {exc.code}') from None


def local_state(executable):
    value = json.loads(subprocess.check_output([executable, 'status', '--json'], timeout=30))
    if value.get('BackendState') != 'Running':
        raise ValueError('Tailscale local status is not Running')
    return value['Self']['ID'], {p['ID']: p for p in value.get('Peer', {}).values()}


def cleanup(scope, api, status, record, apply=False):
    if scope.get('schema') != 'v7_tailscale_expired_ci_cleanup_scope_v1' or scope.get('user_authorized') is not True:
        raise ValueError('Expected explicit authorized cleanup scope')
    own_id, peers = status()
    if own_id != scope.get('self_node_id'):
        raise ValueError('Cleanup scope belongs to a different local node')
    devices = api.call('GET', 'tailnet/-/devices')['devices']
    by_node = {d['nodeId']: d for d in devices}
    initial_ids = set(by_node)
    deleted = set()
    for candidate in scope['candidates']:
        node_id = candidate['ID']
        device = by_node.get(node_id)
        if device is None:
            record({'node_id': node_id, 'state': 'ABSENT_FROM_ADMIN_INVENTORY'})
            continue
        path = 'device/' + urllib.parse.quote(str(device['id']), safe='')
        # Fresh administrative detail and local state immediately before deletion.
        current = api.call('GET', path)
        own_id, peers = status()
        reason = exclusion(candidate, current, peers, own_id, time.time())
        if reason:
            record({'node_id': node_id, 'state': 'SKIPPED', 'reason': reason})
            continue
        routes = api.call('GET', path + '/routes')
        if routes.get('advertisedRoutes') != [] or routes.get('enabledRoutes') != []:
            record({'node_id': node_id, 'state': 'SKIPPED', 'reason': 'ROUTES_PRESENT_OR_UNKNOWN'})
            continue
        record({'node_id': node_id, 'hostname': current['hostname'], 'state': 'DELETE_INTENT' if apply else 'PLANNED'})
        if apply:
            api.call('DELETE', path)
            deleted.add(node_id)
            record({'node_id': node_id, 'state': 'DELETE_ACKNOWLEDGED'})
            time.sleep(0.1)
    remaining = {d['nodeId'] for d in api.call('GET', 'tailnet/-/devices')['devices']}
    result = {'state': 'VERIFIED' if apply else 'PLAN_ONLY', 'deleted': len(deleted),
              'deleted_still_present': sorted(deleted & remaining),
              'unexpected_missing_nodes': sorted((initial_ids - deleted) - remaining)}
    record(result)
    if result['deleted_still_present'] or result['unexpected_missing_nodes']:
        raise ValueError('Final inventory differs from intended deletion set; inspect audit')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--tailscale', default='/Applications/Tailscale.app/Contents/MacOS/Tailscale')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    token = args.token_file.read_text().strip()
    if not token or token.startswith('tskey-auth-'):
        raise ValueError('An administrative API token is required, not an enrollment auth key')
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    with args.audit.open('x') as audit:
        def record(row):
            audit.write(json.dumps({'at_ns': time.time_ns(), **row}) + '\n')
            audit.flush()
            os.fsync(audit.fileno())
        result = cleanup(json.loads(args.scope.read_text()), API(token),
                         lambda: local_state(args.tailscale), record, args.apply)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
