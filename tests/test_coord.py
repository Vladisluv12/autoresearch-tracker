"""Integration tests using independent clones and a real local Git remote."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'coord.py'
REF = 'refs/heads/coordination'


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='coord-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'origin.git'
        self.a = self.root / 'worker-a'
        self.b = self.root / 'worker-b'
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('GIT_', 'COORD_'))}
        self.env.update({
            'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_TERMINAL_PROMPT': '0',
            'GIT_AUTHOR_NAME': 'Tracker Test',
            'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Tracker Test',
            'GIT_COMMITTER_EMAIL': 'test@example.invalid',
        })
        self.git('init', '--bare', '--initial-branch=main', str(self.remote))
        self.git('clone', str(self.remote), str(self.a))
        (self.a / 'README.md').write_text('test repository\n')
        self.git('add', 'README.md', cwd=self.a)
        self.git('commit', '-m', 'Initial commit', cwd=self.a)
        self.git('push', 'origin', 'main', cwd=self.a)
        self.git('clone', str(self.remote), str(self.b))

    def git(self, *args, cwd=None, input=None):
        result = subprocess.run(
            ['git', *args], cwd=cwd or self.root, env=self.env,
            input=input, text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def command(self, *args, worker='a', agent=None, role='unassigned', ok=True):
        env = dict(self.env, COORD_AGENT=agent or f'worker-{worker}', COORD_ROLE=role)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args], cwd=getattr(self, worker),
            env=env, capture_output=True, text=True, timeout=30,
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def concurrent(self, first, second):
        processes = []
        for worker, args in [('a', first), ('b', second)]:
            processes.append(subprocess.Popen(
                [sys.executable, str(SCRIPT), *args], cwd=getattr(self, worker),
                env=dict(self.env, COORD_AGENT=f'worker-{worker}', COORD_ROLE='unassigned'),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        results = []
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                results.append((process.returncode, stdout + stderr))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        return results

    def state(self):
        return json.loads(self.git('--git-dir', str(self.remote), 'show', f'{REF}:state.json'))

    def create(self, title='Task', scope='src', *extra):
        self.command('create', title, '--description', 'Concrete work',
                     '--acceptance', 'Tests pass', '--scope', scope, *extra)
        return sorted(self.state()['tasks'])[-1]

    def age_claim(self, task_id):
        state = self.state()
        state['tasks'][task_id]['heartbeat_at'] = '2000-01-01T00:00:00+00:00'
        checkout = self.root / 'maintenance'
        self.git('clone', '--branch', 'coordination', str(self.remote), str(checkout))
        (checkout / 'state.json').write_text(json.dumps(state))
        self.git('add', 'state.json', cwd=checkout)
        self.git('commit', '-m', 'Simulate an offline worker', cwd=checkout)
        self.git('push', 'origin', 'coordination', cwd=checkout)

    def test_init_race_and_idempotence(self):
        results = self.concurrent(['init'], ['init'])
        self.assertEqual([item[0] for item in results], [0, 0], results)
        before = self.git('--git-dir', str(self.remote), 'rev-parse', REF)
        self.command('init')
        self.assertEqual(before, self.git('--git-dir', str(self.remote), 'rev-parse', REF))
        self.assertEqual(self.state()['tasks'], {})
        self.command('list', worker='b')

    def test_simultaneous_creation_keeps_both_tasks(self):
        self.command('init')
        # Hold both first pushes on the server before either updates the ref.
        # This forces a genuine stale-CAS failure and a successful retry.
        gate = self.remote / 'push-attempts'
        gate.mkdir()
        hook = self.remote / 'hooks' / 'pre-receive'
        hook.write_text(
            '#!/usr/bin/env python3\n'
            'import os, pathlib, sys, time\n'
            f'gate = pathlib.Path({str(gate)!r})\n'
            "(gate / str(os.getpid())).write_text(sys.stdin.read())\n"
            'deadline = time.monotonic() + 10\n'
            'while len(list(gate.iterdir())) < 2:\n'
            '    if time.monotonic() > deadline:\n'
            "        sys.exit('concurrent push did not arrive')\n"
            '    time.sleep(0.02)\n'
        )
        hook.chmod(0o755)
        results = self.concurrent(
            ['create', 'First', '--scope', 'api', '--description', 'A', '--acceptance', 'A works'],
            ['create', 'Second', '--scope', 'web', '--description', 'B', '--acceptance', 'B works'],
        )
        self.assertEqual([item[0] for item in results], [0, 0], results)
        tasks = self.state()['tasks']
        self.assertEqual(len(tasks), 2)
        self.assertEqual({task['title'] for task in tasks.values()}, {'First', 'Second'})
        self.assertEqual(set(tasks), {'T001', 'T002'})
        self.assertGreaterEqual(len(list(gate.iterdir())), 3, 'CAS retry was not exercised')

    def test_same_task_can_only_be_claimed_once(self):
        self.command('init')
        task = self.create()
        results = self.concurrent(['claim', task], ['claim', task])
        self.assertEqual(sum(code == 0 for code, _ in results), 1, results)
        owner = self.state()['tasks'][task]['owner']
        self.assertIn(owner, ['worker-a', 'worker-b'])
        loser = 'b' if owner == 'worker-a' else 'a'
        self.command('heartbeat', task, worker=loser, ok=False)
        self.command('done', task, '--summary', 'Not my task', worker=loser, ok=False)
        self.command('release', task, '--reason', 'Not my task', worker=loser, ok=False)

    def test_scope_locks_are_atomic_and_respect_path_boundaries(self):
        self.command('init')
        parent = self.create('API work', 'src')
        child = self.create('File work', 'src/handler.py')
        results = self.concurrent(['claim', parent], ['claim', child])
        self.assertEqual(sum(code == 0 for code, _ in results), 1, results)
        independent = self.create('Other directory', 'src2')
        self.command('claim', independent)
        whole_repo = self.create('Everything', '*')
        self.command('claim', whole_repo, ok=False)

    def test_lifecycle_dependencies_and_role(self):
        self.command('init')
        first = self.create('API contract', 'contracts')
        second = self.create('Backend', 'server', '--depends-on', first, '--role', 'backend')
        self.command('claim', second, role='backend', ok=False)
        self.command('claim', first)
        self.command('block', first, '--reason', 'Need schema')
        self.assertEqual(self.state()['tasks'][first]['status'], 'blocked')
        conflict = self.create('Conflicting change', 'contracts/schema.json')
        self.command('claim', conflict, worker='b', ok=False)
        self.command('resume', first)
        self.command('heartbeat', first, '--note', 'Schema agreed')
        self.command('handoff', first, '--summary', 'Schema and tests ready',
                     '--pr', 'https://github.com/example/repo/pull/1')
        self.assertEqual(self.state()['tasks'][first]['status'], 'review')
        self.command('claim', conflict, worker='b', ok=False)
        self.command('handoff', first, '--summary', 'Review feedback addressed',
                     '--pr', 'https://github.com/example/repo/pull/1')
        self.assertEqual(self.state()['tasks'][first]['summary'], 'Review feedback addressed')
        self.command('done', first, '--summary', 'Reviewed and merged')
        self.command('claim', second, role='frontend', ok=False)
        self.command('claim', second, role='backend')
        self.command('release', second, '--reason', 'Switching worker', role='backend')
        self.assertEqual(self.state()['tasks'][second]['status'], 'todo')
        self.command('claim', second, worker='b', role='backend')
        self.command('show', second, worker='b')

    def test_takeover_requires_staleness_and_revokes_old_owner(self):
        self.command('init')
        task = self.create()
        self.command('claim', task)
        self.command('takeover', task, '--reason', 'Worker offline', worker='b', ok=False)
        self.age_claim(task)
        self.command('takeover', task, '--reason', 'Worker offline for over 30 minutes', worker='b')
        self.assertEqual(self.state()['tasks'][task]['owner'], 'worker-b')
        self.command('heartbeat', task, ok=False)
        self.command('done', task, '--summary', 'Stale result', ok=False)
        self.command('heartbeat', task, worker='b')
        self.assertIn('Worker offline', json.dumps(self.state()['tasks'][task]['history']))

    def test_tracker_preserves_branch_index_and_worktree(self):
        (self.a / 'README.md').write_text('staged work\n')
        self.git('add', 'README.md', cwd=self.a)
        (self.a / 'README.md').write_text('unstaged work\n')
        (self.a / 'untracked.txt').write_text('untracked work\n')
        def snapshot():
            return (
                self.git('rev-parse', 'HEAD', cwd=self.a),
                self.git('symbolic-ref', 'HEAD', cwd=self.a),
                self.git('diff', '--cached', cwd=self.a),
                self.git('diff', cwd=self.a),
                self.git('status', '--porcelain', cwd=self.a),
                (self.a / 'untracked.txt').read_text(),
            )
        before = snapshot()
        self.command('init')
        task = self.create()
        self.command('claim', task)
        self.command('list')
        self.assertEqual(snapshot(), before)

    def test_invalid_scopes_and_dependencies_do_not_publish_tasks(self):
        self.command('init')
        for scope in ['../escape', '/absolute', 'src/../../escape', 'src/*', '']:
            with self.subTest(scope=scope):
                self.command('create', 'Invalid', '--description', 'No',
                             '--acceptance', 'No', '--scope', scope, ok=False)
        self.command('create', 'Invalid dependency', '--description', 'No',
                     '--acceptance', 'No', '--scope', 'valid', '--depends-on', 'T999', ok=False)
        self.assertEqual(self.state()['tasks'], {})

    def test_split_origin_uses_the_push_repository_for_reads_and_writes(self):
        self.command('init')
        upstream = self.root / 'unrelated-upstream.git'
        self.git('init', '--bare', '--initial-branch=main', str(upstream))
        self.git('remote', 'set-url', 'origin', str(upstream), cwd=self.a)
        self.git('remote', 'set-url', '--push', 'origin', str(self.remote), cwd=self.a)
        task = self.create()
        self.command('claim', task)
        self.command('list')
        self.assertEqual(self.state()['tasks'][task]['owner'], 'worker-a')
        self.assertEqual(self.git('--git-dir', str(upstream), 'for-each-ref'), '')


if __name__ == '__main__':
    unittest.main()
