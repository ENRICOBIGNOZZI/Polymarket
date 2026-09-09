import copy
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from v7_tailscale_ci_cleanup import cleanup, exclusion


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.candidate = {'ID': 'runner1', 'HostName': 'github-runnervmgx7h7'}
        self.peer = {**self.candidate, 'Online': False, 'Expired': True}
        self.device = {'id': '123', 'nodeId': 'runner1', 'hostname': self.candidate['HostName'],
                       'expires': '2026-01-01T00:00:00Z', 'lastSeen': '2026-01-01T00:00:00Z',
                       'keyExpiryDisabled': False, 'isExternal': False, 'tags': []}

    def test_excludes_production_online_unknown_expiry_and_changed_identity(self):
        self.assertIsNone(exclusion(self.candidate, self.device, {'runner1': self.peer}, 'self', time.time()))
        for field, value in [('hostname', 'paper-server'), ('nodeId', 'other'),
                             ('keyExpiryDisabled', True), ('expires', None),
                             ('lastSeen', None), ('tags', ['tag:server']), ('isExternal', True)]:
            self.assertIsNotNone(exclusion(self.candidate, {**self.device, field: value},
                                          {'runner1': self.peer}, 'self', time.time()), field)
        self.assertIsNotNone(exclusion(self.candidate, self.device,
                                      {'runner1': {**self.peer, 'Online': True}}, 'self', time.time()))
        self.assertIsNotNone(exclusion(self.candidate, self.device, {'runner1': self.peer}, 'runner1', time.time()))

    def test_plan_and_apply_exact_ids_preserve_production(self):
        for apply in [False, True]:
            devices = [copy.deepcopy(self.device), {'id': '456', 'nodeId': 'production'}]
            calls = []
            class FakeAPI:
                def call(_, method, path):
                    calls.append((method, path))
                    if path == 'tailnet/-/devices': return {'devices': copy.deepcopy(devices)}
                    if path == 'device/123/routes': return {'advertisedRoutes': [], 'enabledRoutes': []}
                    if method == 'DELETE':
                        self.assertEqual(path, 'device/123')
                        devices[:] = [d for d in devices if d['id'] != '123']
                        return {}
                    return copy.deepcopy(self.device)
            scope = {'schema': 'v7_tailscale_expired_ci_cleanup_scope_v1', 'user_authorized': True,
                     'self_node_id': 'self', 'candidates': [self.candidate]}
            records = []
            result = cleanup(scope, FakeAPI(), lambda: ('self', {'runner1': self.peer}), records.append, apply)
            self.assertEqual(result['deleted'], int(apply))
            self.assertEqual(result['unexpected_missing_nodes'], [])
            self.assertEqual(sum(method == 'DELETE' for method, _ in calls), int(apply))
            self.assertIn('production', [d['nodeId'] for d in devices])


if __name__ == '__main__': unittest.main()
