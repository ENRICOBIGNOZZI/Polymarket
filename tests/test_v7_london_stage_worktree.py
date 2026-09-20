from __future__ import annotations
import os
import shutil
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LondonStageWorktreeTests(unittest.TestCase):
    def run_preflight(self, script, source, sha):
        # Exercise source guards only; never invoke build/removal/service actions.
        prefix = script.split('mkdir -p "$RUNTIME_ROOT/by-sha"', 1)[0]
        self.assertNotIn('cmake', prefix)
        self.assertNotIn('rm -rf', prefix)
        # This test is platform-independent. Production retains its Linux guard.
        prefix = prefix.replace('$(uname -s)', 'Linux')
        service_user = subprocess.check_output(['id', '-un'], text=True).strip()
        env = dict(os.environ, POLYMARKET_EXPECTED_SHA=sha, POLYMARKET_APP_DIR=str(source),
                   POLYMARKET_SERVICE_USER=service_user,
                   POLYMARKET_LONDON_DEPLOY_LOCK_FILE=str(source.parent / "deploy.lock"))
        # macOS lacks the flock CLI. Exercise the same OS lock, not a no-op,
        # in this source-guard unit fixture; production still requires flock.
        if shutil.which('flock') is None:
            tools = source.parent / 'test-lock-tools'
            tools.mkdir(exist_ok=True)
            helper = tools / 'flock'
            helper.write_text('#!' + sys.executable + '\nimport fcntl,sys\n'
                              'assert sys.argv[1] == "-n"\n'
                              'try: fcntl.flock(int(sys.argv[2]), fcntl.LOCK_EX | fcntl.LOCK_NB)\n'
                              'except BlockingIOError: raise SystemExit(1)\n')
            helper.chmod(0o700)
            env['PATH'] = str(tools) + os.pathsep + env.get('PATH','')
        return subprocess.run(['bash', '-c', prefix], env=env, capture_output=True, text=True)

    def test_linked_worktree_and_normal_checkout_pass_while_dirty_fails(self):
        script = (ROOT / 'ops/v7_london_stage_release.sh').read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); repository = root / 'repo'; worktree = root / 'linked'
            subprocess.run(['git', 'init', '-q', str(repository)], check=True)
            (repository / 'source.txt').write_text('original')
            subprocess.run(['git', '-C', str(repository), 'add', 'source.txt'], check=True)
            subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'fixture'], check=True)
            sha = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip()
            subprocess.run(['git', '-C', str(repository), 'worktree', 'add', '--detach', str(worktree), sha],
                           check=True, capture_output=True)
            self.assertTrue((worktree / '.git').is_file())
            for checkout in (repository, worktree):
                result = self.run_preflight(script, checkout, sha)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('GIT_SOURCE=(git -c "safe.directory=$SOURCE_DIR" -C "$SOURCE_DIR")', script)
            old = script.replace('[[ -e "$SOURCE_DIR/.git" && "$("${GIT_SOURCE[@]}" rev-parse --is-inside-work-tree 2>/dev/null)" == true ]]',
                                 '[[ -d "$SOURCE_DIR/.git" ]]')
            self.assertEqual(self.run_preflight(old, worktree, sha).returncode, 66)
            (worktree / 'source.txt').write_text('dirty')
            result = self.run_preflight(script, worktree, sha)
            self.assertEqual(result.returncode, 66)
            self.assertIn('dirty source checkout', result.stderr)
            self.assertEqual(self.run_preflight(script, root, sha).returncode, 66)


if __name__ == '__main__':
    unittest.main()
