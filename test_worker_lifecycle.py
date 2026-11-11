"""Worker lifecycle regressions. Synthetic only; no accounts, HTTP or browser.
Real Docker/PID1 test: sudo -n env RUN_WORKER_INTEGRATION=1 /usr/bin/python3
    -m unittest -v test_worker_lifecycle
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import patch

import rainyun_http as m

PINNED_IMAGE = 'sha256:a01a541ef0c389536eeb772ac36b7f60e28363d4b2d5bb6ed88c155c4da62490'


def docker_ids(name):
    result = subprocess.run(
        ['/usr/bin/docker', 'container', 'ls', '--all', '--filter',
         'name=^/' + name + '$', '--format', '{{.ID}}'],
        env=m.DOCKER_ENV, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, timeout=10, check=True)
    return result.stdout.strip()


class LifecycleCommands:
    """Fault-injection seam only; real Docker/PID1 is tested separately below."""
    def __init__(self, fault=None):
        self.calls, self.fault = [], fault

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[0] == '/usr/bin/systemd-run':
            return subprocess.CompletedProcess(command, 0)
        if command[:3] == ['/usr/bin/systemctl', '--system', 'list-units']:
            return subprocess.CompletedProcess(command, 0, stdout='[]')
        if command[:3] == ['/usr/bin/systemctl', '--system', 'show']:
            unit = command[3].removesuffix('.timer')
            return subprocess.CompletedProcess(command, 0, stdout=
                f'LoadState=loaded\nActiveState=active\nTriggers={unit}.service\n')
        if command[:2] == ['docker', 'run']:
            kwargs['stdout'].write(b'{}')
        if command[:2] == ['docker', 'rm'] and self.fault in ('rm_oserror', 'rm_timeout'):
            if self.fault == 'rm_oserror':
                raise OSError('synthetic daemon failure')
            raise subprocess.TimeoutExpired(command, 10)
        if command[:3] == ['docker', 'container', 'ls']:
            if self.fault == 'ls_oserror':
                raise OSError('synthetic daemon failure')
            if self.fault == 'ls_timeout':
                raise subprocess.TimeoutExpired(command, 10)
            if self.fault == 'ls_nonzero':
                return subprocess.CompletedProcess(command, 1, stdout='')
            if self.fault == 'still_present':
                return subprocess.CompletedProcess(command, 0, stdout='f00df00df00d\n')
        return subprocess.CompletedProcess(command, 0, stdout='')


class TestCleanupConfirmation(unittest.TestCase):
    def test_normal_return_cancels_reaper_only_after_successful_exact_absence_check(self):
        runner = LifecycleCommands()
        with m.PrivateWorkspace() as directory, patch.object(m.subprocess, 'run', side_effect=runner):
            self.assertEqual(m.Worker(PINNED_IMAGE).run('node', [], [], directory), {})
            self.assertEqual(list(directory.iterdir()), [])
        commands = [command for command, _ in runner.calls]
        self.assertEqual([command[:2] for command in commands], [
            ['/usr/bin/systemd-run', '--system'], ['/usr/bin/systemctl', '--system'],
            ['docker', 'run'], ['docker', 'rm'], ['docker', 'container'],
            ['/usr/bin/systemctl', '--system'], ['/usr/bin/systemctl', '--system']])
        registered, _, docker, removed, listed, stopped, verified = commands
        name = docker[docker.index('--name') + 1]
        self.assertRegex(name, r'^rainyun-isolated-[0-9a-f]{32}$')
        self.assertEqual(registered[-4:], ['/usr/bin/docker', 'rm', '--force', name])
        self.assertEqual(removed, ['docker', 'rm', '--force', name])
        self.assertEqual(listed, ['docker', 'container', 'ls', '--all', '--filter',
                                 'name=^/' + name + '$', '--format', '{{.ID}}'])
        unit = 'rainyun-worker-cleanup-' + name.removeprefix('rainyun-isolated-')
        self.assertEqual(stopped, ['/usr/bin/systemctl', '--system', 'stop',
                                  unit + '.timer', unit + '.service'])
        self.assertEqual(verified, ['/usr/bin/systemctl', '--system', 'list-units',
                                   '--all', '--no-pager', '--output=json',
                                   unit + '.timer', unit + '.service'])
        for option in ('--collect', '--on-active=40s', '--timer-property=AccuracySec=1s',
                       '--timer-property=RemainAfterElapse=no', '--property=Type=oneshot',
                       '--property=User=root', '--property=Group=root',
                       '--property=TimeoutStartSec=15s', '--property=TimeoutStopSec=5s',
                       '--property=KillMode=control-group'):
            self.assertIn(option, registered)
        for _, kwargs in runner.calls:
            self.assertEqual(kwargs['env'], m.DOCKER_ENV)
            self.assertLessEqual(kwargs['timeout'], 25)

    def test_unconfirmed_cleanup_is_critical_and_aborts_batch_without_canceling_reaper(self):
        for fault in ('ls_nonzero', 'ls_timeout', 'ls_oserror', 'still_present',
                      'rm_oserror', 'rm_timeout'):
            runner = LifecycleCommands(fault)
            accounts_started = []
            with self.subTest(fault=fault), m.PrivateWorkspace() as directory:
                class SyntheticAccount:
                    def run(self, *args):
                        accounts_started.append(True)
                        m.Worker(PINNED_IMAGE).run('node', [], [], directory)
                        return 'already_signed'
                with patch.object(m.subprocess, 'run', side_effect=runner), \
                        patch.object(m, 'read_credentials', return_value=(None, None)):
                    report, code = m.run_accounts([Path('/unused/1'), Path('/unused/2')],
                                                 account_factory=SyntheticAccount)
                self.assertEqual(len(accounts_started), 1, 'batch continued after uncertain cleanup')
                self.assertEqual((code, report['total'], report['failed']), (1, 1, 1))
                self.assertTrue(report['aborted'])
                self.assertEqual(report['results'][0]['error_stage'], 'worker_cleanup')
                self.assertFalse(any('stop' in command for command, _ in runner.calls))
                self.assertFalse(list(directory.iterdir()), 'private stdout leaked on cleanup error')
            # Also assert that Worker itself raises the dedicated severity class.
            with m.PrivateWorkspace() as directory, patch.object(m.subprocess, 'run',
                    side_effect=LifecycleCommands(fault)):
                with self.assertRaises(m.CriticalFailure) as error:
                    m.Worker(PINNED_IMAGE).run('node', [], [], directory)
                self.assertEqual(error.exception.stage, 'worker_cleanup')


class TestReaperCancellation(unittest.TestCase):
    def test_already_collected_units_are_success_even_when_stop_returns_nonzero(self):
        cleanup = m.WorkerCleanup('rainyun-isolated-' + 'a' * 32)
        responses = [subprocess.CompletedProcess([], 5),
                     subprocess.CompletedProcess([], 0, stdout='[]')]
        with patch.object(m.subprocess, 'run', side_effect=responses):
            cleanup.cancel()

    def test_active_or_unreadable_unit_state_never_reports_cancellation_success(self):
        cleanup = m.WorkerCleanup('rainyun-isolated-' + 'a' * 32)
        states = [subprocess.CompletedProcess([], 1, stdout='[]'),
                  subprocess.CompletedProcess([], 0, stdout='not json'),
                  subprocess.CompletedProcess([], 0, stdout='{}'),
                  subprocess.CompletedProcess([], 0, stdout=json.dumps([
                      {'unit': cleanup.unit + '.timer', 'load': 'loaded', 'active': 'active'}])),
                  subprocess.CompletedProcess([], 0, stdout=json.dumps([
                      {'unit': 'unrelated.service', 'load': 'loaded', 'active': 'inactive'}]))]
        for state in states:
            with self.subTest(state=state.stdout), patch.object(m.subprocess, 'run',
                    side_effect=[subprocess.CompletedProcess([], 5), state]):
                with self.assertRaises(m.CriticalFailure):
                    cleanup.cancel()


class TestReaperRegistration(unittest.TestCase):
    def test_worker_has_fixed_site_label_for_future_site_scoped_stop_post(self):
        command = m.Worker(PINNED_IMAGE).command('node', [], [],
                                               'rainyun-isolated-' + 'a' * 32)
        self.assertIn('--label=hermes.signin.site=rainyun', command)
        self.assertEqual(sum(value.startswith('--label') for value in command), 1)

    def test_registration_failure_never_starts_docker(self):
        for failure in (1, OSError('offline fault'), subprocess.TimeoutExpired('systemd-run', 5)):
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                if command[0] == '/usr/bin/systemd-run':
                    if isinstance(failure, Exception):
                        raise failure
                    return subprocess.CompletedProcess(command, failure)
                if command[:2] == ['docker', 'run']:
                    kwargs['stdout'].write(b'{}')
                return subprocess.CompletedProcess(command, 0, stdout='')
            with self.subTest(failure=type(failure).__name__), m.PrivateWorkspace() as directory:
                with patch.object(m.subprocess, 'run', side_effect=run):
                    with self.assertRaises(m.Failure) as error:
                        m.Worker(PINNED_IMAGE).run('node', [], ['-e', ''], directory)
                self.assertEqual(type(error.exception).__name__, 'CriticalFailure')
                self.assertEqual(error.exception.stage, 'worker_reaper')
                self.assertFalse(any(command[0] in ('docker', '/usr/bin/docker') for command in calls))
                self.assertFalse(any('stop' in command for command in calls))


@unittest.skipUnless(os.getenv('RUN_WORKER_INTEGRATION') == '1' and os.geteuid() == 0,
                     'root + explicit offline Docker/systemd opt-in required')
class TestIndependentReaper(unittest.TestCase):
    def assert_units_collected(self, unit):
        deadline = time.monotonic() + 5
        while True:
            result = subprocess.run(['/usr/bin/systemctl', '--system', 'list-units',
                '--all', '--no-pager', '--output=json', unit + '.timer', unit + '.service'],
                env=m.DOCKER_ENV, capture_output=True, text=True, timeout=5, check=True)
            if json.loads(result.stdout) == []:
                return
            self.assertLess(time.monotonic(), deadline, 'UUID cleanup units were not collected')
            time.sleep(0.1)

    def emergency_cleanup(self, name, unit):
        # Each target is an exact UUID allocated by THIS test, never a site sweep.
        if docker_ids(name):
            subprocess.run(['/usr/bin/docker', 'rm', '--force', name],
                           env=m.DOCKER_ENV, capture_output=True, timeout=15, check=True)
        self.assertEqual(docker_ids(name), '', 'emergency cleanup failed')
        subprocess.run(['/usr/bin/systemctl', '--system', 'stop', unit + '.timer', unit + '.service'],
                       env=m.DOCKER_ENV, capture_output=True, timeout=15)
        self.assert_units_collected(unit)

    def test_normal_return_has_no_container_or_cleanup_units(self):
        nonce = uuid.uuid4().hex
        name, unit = 'rainyun-isolated-' + nonce, 'rainyun-worker-cleanup-' + nonce
        with m.PrivateWorkspace() as directory:
            try:
                with patch.object(m.uuid, 'uuid4', return_value=uuid.UUID(hex=nonce)):
                    result = m.Worker(PINNED_IMAGE).run('node', [],
                        ['-e', 'process.stdout.write(JSON.stringify({fixture:true}))'], directory)
                self.assertEqual(result, {'fixture': True})
                self.assertEqual(docker_ids(name), '')
                self.assert_units_collected(unit)
                self.assertEqual(list(directory.iterdir()), [])
                print(json.dumps({'test': 'normal_return', 'container': name,
                    'container_absent': True, 'cleanup_units_collected': True,
                    'emergency_cleanup_needed': False}), flush=True)
            finally:
                self.emergency_cleanup(name, unit)

    def test_timeout_has_no_container_or_cleanup_units(self):
        nonce = uuid.uuid4().hex
        name, unit = 'rainyun-isolated-' + nonce, 'rainyun-worker-cleanup-' + nonce
        with m.PrivateWorkspace() as directory:
            try:
                with patch.object(m.uuid, 'uuid4', return_value=uuid.UUID(hex=nonce)):
                    with self.assertRaises(m.Failure) as error:
                        m.Worker(PINNED_IMAGE, timeout=1).run('node', [],
                            ['-e', "process.on('SIGTERM',()=>{});setInterval(()=>{},1000)"], directory)
                self.assertEqual(error.exception.stage, 'worker_timeout')
                self.assertEqual(docker_ids(name), '')
                self.assert_units_collected(unit)
                self.assertEqual(list(directory.iterdir()), [])
                print(json.dumps({'test': 'worker_timeout', 'container': name,
                    'container_absent': True, 'cleanup_units_collected': True,
                    'emergency_cleanup_needed': False}), flush=True)
            finally:
                self.emergency_cleanup(name, unit)

    def test_sigkill_host_process_group_is_reaped_by_pid1_before_deadline(self):
        nonce = uuid.uuid4().hex
        name = 'rainyun-isolated-' + nonce
        unit = 'rainyun-worker-cleanup-' + nonce
        # Only the UUID is made deterministic, so the observer can query exactly
        # this container. Worker.run and Docker/systemd are not mocked.
        host = '''import sys,types
from pathlib import Path
import rainyun_http as m
m.uuid.uuid4 = lambda: types.SimpleNamespace(hex=sys.argv[2])
m.Worker(sys.argv[3]).run('node', [],
    ['-e', "process.on('SIGTERM',()=>{});setInterval(()=>{},1000)"],
    Path(sys.argv[1]))
'''
        process = None
        started = time.monotonic()
        with m.PrivateWorkspace() as directory:
            try:
                process = subprocess.Popen(
                    [sys.executable, '-c', host, str(directory), nonce, PINNED_IMAGE],
                    cwd=m.ROOT, env=m.DOCKER_ENV, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                startup_deadline = started + 12
                while not docker_ids(name):
                    self.assertIsNone(process.poll(), 'synthetic host exited before container started')
                    self.assertLess(time.monotonic(), startup_deadline, 'container did not start')
                    time.sleep(0.1)
                inspect = subprocess.run(
                    ['/usr/bin/docker', 'inspect', name], env=m.DOCKER_ENV,
                    capture_output=True, text=True, timeout=10, check=True)
                config = json.loads(inspect.stdout)[0]
                self.assertTrue(config['State']['Running'])
                self.assertEqual(config['HostConfig']['NetworkMode'], 'none')
                self.assertTrue(config['HostConfig']['ReadonlyRootfs'])
                self.assertEqual(config['Config']['User'], '65534:65534')
                self.assertEqual(config['Config']['Labels']['hermes.signin.site'], 'rainyun')
                self.assertEqual(config['Mounts'], [])
                self.assertEqual(Path('/proc/1/comm').read_text().strip(), 'systemd')
                service = subprocess.run(['/usr/bin/systemctl', '--system', 'show', unit + '.service',
                    '--property=User,Group,Type,TimeoutStartUSec,TimeoutStopUSec,KillMode,ExecStart'],
                    env=m.DOCKER_ENV, capture_output=True, text=True, timeout=5, check=True)
                properties = dict(line.split('=', 1) for line in service.stdout.splitlines() if '=' in line)
                for key, value in {'User': 'root', 'Group': 'root', 'Type': 'oneshot',
                        'TimeoutStartUSec': '15s', 'TimeoutStopUSec': '5s',
                        'KillMode': 'control-group'}.items():
                    self.assertEqual(properties[key], value)
                self.assertIn('path=/usr/bin/docker', properties['ExecStart'])
                self.assertIn('argv[]=/usr/bin/docker rm --force ' + name, properties['ExecStart'])
                timer = subprocess.run(['/usr/bin/systemctl', '--system', 'show', unit + '.timer',
                    '--property=ActiveState,AccuracyUSec,RemainAfterElapse,Triggers'],
                    env=m.DOCKER_ENV, capture_output=True, text=True, timeout=5, check=True)
                timer_properties = dict(line.split('=', 1) for line in timer.stdout.splitlines() if '=' in line)
                self.assertEqual(timer_properties, {'ActiveState': 'active', 'AccuracyUSec': '1s',
                    'RemainAfterElapse': 'no', 'Triggers': unit + '.service'})
                ps = subprocess.run(['/usr/bin/ps', '-eo', 'pid=,pgid=,comm='],
                    capture_output=True, text=True, timeout=5, check=True)
                group = [line.split() for line in ps.stdout.splitlines()
                         if len(line.split()) == 3 and int(line.split()[1]) == process.pid]
                self.assertTrue(any(row[2] == 'docker' for row in group), 'docker CLI was not in victim group')
                # Kill Python AND its docker CLI process group, not only the
                # Python process. PID1's root reaper must be outside this group.
                os.killpg(process.pid, signal.SIGKILL)
                self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
                killed = time.monotonic()
                self.assertTrue(docker_ids(name), 'container unexpectedly died with the host')
                while docker_ids(name) and time.monotonic() < started + 58:
                    time.sleep(0.25)
                self.assertEqual(docker_ids(name), '',
                                 'daemon container survived SIGKILL beyond independent cleanup deadline')
                self.assert_units_collected(unit)
                print(json.dumps({'test': 'host_process_group_sigkill', 'host_exit': process.returncode,
                                  'victim_group': group, 'root_pid1_reaper_verified': True,
                                  'cleanup_units_collected': True, 'emergency_cleanup_needed': False,
                                  'container': name, 'removed': True,
                                  'elapsed_seconds': round(time.monotonic() - started, 3),
                                  'after_kill_seconds': round(time.monotonic() - killed, 3),
                                  'image': PINNED_IMAGE, 'accounts_used': False,
                                  'browser_used': False, 'model_used': False}), flush=True)
            finally:
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                # Emergency hygiene only, AFTER all success assertions above.
                self.emergency_cleanup(name, unit)


if __name__ == '__main__':
    unittest.main()
