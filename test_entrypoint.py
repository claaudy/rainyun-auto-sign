"""Local entrypoint regressions: no accounts, HTTP, browser, Docker or systemd."""
import runpy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
ENTRYPOINT = ROOT / 'rainyun_signin.py'


class TestEntrypoint(unittest.TestCase):
    def test_only_main_execution_delegates_with_original_cli_arguments(self):
        self.assertTrue(ENTRYPOINT.is_file(), 'default HTTP launcher is missing')
        import rainyun_http
        # runpy sets argv[0] to this path, just like direct script execution.
        arguments = [str(ENTRYPOINT), '--future-option', 'literal :=$() 空 格',
                     '--worker-image', 'sha256:' + 'a' * 64, '--status-only']
        seen = []

        def http_main():
            seen.append(list(sys.argv))
            return 23

        # Mock only the action boundary: the real main may read host accounts.
        with patch.object(rainyun_http, 'main', side_effect=http_main) as delegate, \
                patch.object(sys, 'argv', arguments):
            runpy.run_path(str(ENTRYPOINT), run_name='release_import_only')
            delegate.assert_not_called()
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(ENTRYPOINT), run_name='__main__')
        self.assertEqual(stopped.exception.code, 23)
        self.assertEqual(seen, [arguments])
        delegate.assert_called_once_with()

    def test_real_import_and_help_need_only_stdlib_and_never_start_automation(self):
        import subprocess
        guard = r'''
import importlib.abc
from pathlib import Path
import runpy
import sys
root, mode = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(root))
class NoAutomationImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0].lower() in {
            'playwright', 'selenium', 'browser_use', 'drissionpage', 'pyppeteer',
            'openai', 'anthropic', 'requests', 'numpy', 'cv2', 'tenvision_raw'}:
            raise AssertionError('forbidden automation import: ' + fullname)
sys.meta_path.insert(0, NoAutomationImports())
def deny_actions(event, args):
    if event in {'socket.__new__', 'socket.connect', 'subprocess.Popen',
                 'os.system', 'os.posix_spawn', 'os.fork', 'os.exec'}:
        raise AssertionError('forbidden external action: ' + event)
    if event == 'open' and isinstance(args[0], (str, bytes)):
        name = str(args[0])
        if name.endswith('.env') or 'accounts/' in name:
            raise AssertionError('credential access is forbidden in entrypoint tests')
sys.addaudithook(deny_actions)
entrypoint = root / 'rainyun_signin.py'
sys.argv = [str(entrypoint)] + (['--help'] if mode == 'help' else [])
try:
    runpy.run_path(str(entrypoint), run_name='__main__' if mode == 'help' else 'import_only')
except SystemExit as stopped:
    if mode != 'help' or stopped.code not in (0, None):
        raise
else:
    if mode == 'help':
        raise AssertionError('--help failed to stop before account actions')
    print('IMPORT_OK')
'''
        for mode in ('import', 'help'):
            with self.subTest(mode=mode):
                result = subprocess.run(
                    [sys.executable, '-I', '-S', '-B', '-c', guard, str(ROOT), mode],
                    cwd=ROOT, env={'PATH': '/usr/bin:/bin', 'RUN_WORKER_INTEGRATION': '0'},
                    capture_output=True, text=True, timeout=10, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stderr, '')
                if mode == 'import':
                    self.assertEqual(result.stdout, 'IMPORT_OK\n')
                else:
                    for option in ('--env', '--env-dir', '--worker-image', '--status-only'):
                        self.assertIn(option, result.stdout)
                    self.assertIn('HTTP-only', result.stdout)


if __name__ == '__main__':
    unittest.main()
