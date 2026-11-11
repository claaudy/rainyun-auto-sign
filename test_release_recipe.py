"""Static release recipes only: never invoke Docker, systemd or the network."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent


class TestReleaseRecipe(unittest.TestCase):
    def test_default_dockerfile_is_the_browser_free_worker_recipe(self):
        default = ROOT / 'Dockerfile'
        self.assertTrue(default.is_file(), 'default worker Dockerfile is missing')
        self.assertEqual(default.read_bytes(), (ROOT / 'Dockerfile.worker').read_bytes())
        instructions = [line.strip() for line in default.read_text().splitlines()
                        if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual([line for line in instructions if line.startswith(('COPY ', 'ADD '))],
                         ['COPY --from=node /usr/local/bin/node /usr/local/bin/node'])
        for forbidden in ('playwright', 'chromium', 'selenium', 'rainyun_signin',
                          'accounts', '.env', 'docker.sock', 'dbus', '--privileged'):
            self.assertNotIn(forbidden, default.read_text().lower())
        self.assertIn('USER 65534:65534', instructions)
        self.assertEqual((ROOT / '.dockerignore').read_text().strip(), '*\n!Dockerfile.worker')

    def test_compose_only_builds_a_credential_free_worker_and_refuses_signin(self):
        import ast
        import json
        compose = ROOT / 'docker-compose.yml'
        self.assertTrue(compose.is_file(), 'build-only Compose recipe is missing')
        lines = [line for line in compose.read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual(lines[:2], ['services:', '  rainyun-worker:'])
        properties = dict(line.strip().split(':', 1) for line in lines[2:]
                          if line.startswith('    ') and not line.startswith('      '))
        properties = {key: value.strip() for key, value in properties.items()}
        self.assertEqual(set(properties), {'build', 'image', 'profiles', 'entrypoint',
                         'command', 'restart', 'network_mode', 'read_only', 'user',
                         'cap_drop', 'security_opt', 'pids_limit', 'mem_limit', 'cpus', 'logging'})
        self.assertEqual([line.strip() for line in lines[2:] if line.startswith('      ')],
                         ['context: .', 'dockerfile: Dockerfile', 'driver: none'])
        self.assertEqual(properties['build'], '')
        self.assertEqual(properties['logging'], '')
        self.assertEqual(properties['image'], 'signin-rainyun-http-worker:takeover')
        self.assertEqual(json.loads(properties['profiles']), ['build-only'])
        self.assertEqual(json.loads(properties['entrypoint']), ['python3', '-c'])
        self.assertEqual(json.loads(properties['restart']), 'no')
        self.assertEqual(properties['network_mode'], 'none')
        self.assertEqual(properties['read_only'], 'true')
        self.assertEqual(json.loads(properties['user']), '65534:65534')
        self.assertEqual(json.loads(properties['cap_drop']), ['ALL'])
        self.assertEqual(json.loads(properties['security_opt']), ['no-new-privileges:true'])
        self.assertEqual(properties['pids_limit'], '64')
        self.assertEqual(properties['mem_limit'], '256m')
        self.assertEqual(properties['cpus'], '1.0')
        commands = json.loads(properties['command'])
        self.assertEqual(len(commands), 1)
        body = ast.parse(commands[0]).body
        self.assertEqual(len(body), 1)
        self.assertIsInstance(body[0], ast.Raise)
        self.assertIsInstance(body[0].exc, ast.Call)
        self.assertIsInstance(body[0].exc.func, ast.Name)
        self.assertEqual(body[0].exc.func.id, 'SystemExit')
        self.assertEqual(body[0].exc.keywords, [])
        self.assertEqual(len(body[0].exc.args), 1)
        self.assertIsInstance(body[0].exc.args[0], ast.Constant)
        self.assertIn('rainyun_signin.py', body[0].exc.args[0].value)
        self.assertIn('host', body[0].exc.args[0].value)
        with self.assertRaises(SystemExit) as stopped:
            exec(compile(commands[0], '<compose-build-only-guard>', 'exec'), {})
        self.assertIsInstance(stopped.exception.code, str)

    def test_gitignore_keeps_accounts_runtime_caches_and_proofs_private(self):
        ignore = ROOT / '.gitignore'
        self.assertTrue(ignore.is_file(), 'release .gitignore is missing')
        patterns = {line.strip() for line in ignore.read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith('#')}
        required = {'.runtime/', 'accounts/', '.env', '.env.*', '*.env',
                    '.venv/', '__pycache__/', '*.pyc', '*.pyo', '.cache/',
                    '.pytest_cache/', '.mypy_cache/', '.ruff_cache/', '*.log',
                    '.release-evidence/', 'test-results*', 'verification.json',
                    '*captcha*.json', '*proof*.json', '*collect*.json',
                    '*ticket*.json', '*sess*.json', 'background.png', 'sprite.png',
                    'tdc.js', 'tdc-*.js', 'proofs/', 'screenshots/'}
        self.assertLessEqual(required, patterns)
        self.assertFalse(any(pattern.startswith('!') for pattern in patterns),
                         'release rules must not reinclude private runtime material')
        import fnmatch
        for source in ('rainyun_signin.py', 'rainyun_http.py', 'tdc_runner.js',
                       'tenvision_raw.py', 'LICENSE-TenVision', 'Dockerfile',
                       'Dockerfile.worker', '.dockerignore', 'docker-compose.yml',
                       'README.md', 'test_entrypoint.py', 'test_release_recipe.py',
                       'test_rainyun.py', 'test_worker_integration.py', 'test_worker_lifecycle.py'):
            self.assertFalse(any(fnmatch.fnmatchcase(source, pattern)
                                 for pattern in patterns if not pattern.endswith('/')), source)


if __name__ == '__main__':
    unittest.main()
