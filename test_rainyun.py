"""OFFLINE TEST FIXTURES ONLY: never account/API evidence."""
import importlib.util
import unittest


def candidate():
    assert importlib.util.find_spec('rainyun_http') is not None, 'HTTP CLI module missing'
    import rainyun_http
    return rainyun_http


class TestProtocol(unittest.TestCase):
    def test_rys_matches_javascript_decimal_format(self):
        m = candidate()
        cases = {1700000000000: '1700000000', 1700000000100: '1700000000.1',
                 1700000000010: '1700000000.01', 1700000000001: '1700000000.001',
                 1700000000999: '1700000000.999'}
        for value, expected in cases.items():
            self.assertEqual(m.rys_header(value), expected)


class TestTransport(unittest.TestCase):
    def test_https_transport_refuses_unexpected_origins_and_redirects(self):
        m = candidate()
        self.assertTrue(hasattr(m, 'Transport'), 'guarded transport missing')
        t = m.Transport('api')
        for url in ['http://api.v2.rainyun.com/user/login',
                    'https://api.v2.rainyun.com.evil.test/',
                    'https://api.v2.rainyun.com:444/',
                    'https://x@api.v2.rainyun.com/',
                    'https://turing.captcha.qcloud.com/']:
            with self.subTest(url=url), self.assertRaises(m.Failure):
                t.request('POST', url, {'password': 'FIXTURE_PASSWORD'})
        import urllib.request
        req = urllib.request.Request('https://api.v2.rainyun.com/user/login', data=b'FIXTURE_PASSWORD')
        with self.assertRaises(m.Failure):
            m.NoRedirect().redirect_request(req, None, 307, '', {}, 'https://evil.test/')

    def test_transport_uses_timeouts_and_parses_json_without_logging(self):
        m = candidate()
        self.assertTrue(hasattr(m, 'Transport'), 'guarded transport missing')
        import io
        from unittest.mock import patch
        class Response(io.BytesIO):
            status = 200
        class Opener:
            def open(self, request, timeout):
                self.request, self.timeout = request, timeout
                return Response(b'{"code":200,"data":"FIXTURE_SECRET"}')
        t = m.Transport('api', timeout=7)
        t.opener = Opener()
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            status, body = t.request('POST', m.API + '/user/login', {'field': 'FIXTURE_USER', 'password': 'FIXTURE_PASSWORD'})
        self.assertEqual((status, body['code']), (200, 200))
        self.assertEqual(t.opener.timeout, 7)
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(t.opener.request.get_header('Origin'), m.APP)


class FixtureAPI:
    """Entirely fake offline responses; no network/cookies."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.session = True
    def request(self, method, url, payload=None, **kwargs):
        self.calls.append((method, url, payload))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    def has_session(self):
        return self.session
    def install_csrf(self, token):
        self.token = token


def fixture_prefix(login_code=200):
    return [(200, b'window.fea = 0'), (200, {'code': login_code}),
            (200, {'code': 200, 'data': 'FIXTURE_CSRF'})]


def fixture_task(status):
    return (200, {'code': 200, 'data': [{'Name': '每日签到', 'Status': status}]})


class FixtureCaptcha:
    def __init__(self):
        self.calls = 0
    def solve(self, purpose):
        self.calls += 1
        return {'ticket': 'FIXTURE_TICKET', 'randstr': 'FIXTURE_RANDSTR'}


class TestAccount(unittest.TestCase):
    def run_fixture(self, replies, status_only=False):
        m = candidate()
        self.assertTrue(hasattr(m, 'Account'), 'account workflow missing')
        api, captcha = FixtureAPI(replies), FixtureCaptcha()
        result = m.Account(api, captcha).run('FIXTURE_USER', 'FIXTURE_PASSWORD', status_only)
        return result, api, captcha

    def test_already_signed_never_requests_captcha_or_posts_task(self):
        result, api, captcha = self.run_fixture(fixture_prefix() + [fixture_task(2)])
        self.assertEqual(result, 'already_signed')
        self.assertEqual(captcha.calls, 0)
        self.assertEqual(sum(c[0] == 'POST' for c in api.calls), 1)

    def test_status_only_reads_pending_without_signing(self):
        result, api, captcha = self.run_fixture(fixture_prefix() + [fixture_task(1)], True)
        self.assertEqual(result, 'pending')
        self.assertEqual(captcha.calls, 0)

    def test_signin_requires_final_signed_readback(self):
        m = candidate()
        self.assertTrue(hasattr(m, 'Account'), 'account workflow missing')
        result, api, captcha = self.run_fixture(fixture_prefix() + [fixture_task(1), (200, {'code': 200}), fixture_task(2)])
        self.assertEqual(result, 'signed')
        self.assertEqual(api.calls[-1][:2], ('GET', m.API + '/user/reward/tasks'))
        self.assertEqual(api.calls[-2][2], {'task_name': '每日签到', 'verifyCode': '', 'vticket': 'FIXTURE_TICKET', 'vrandstr': 'FIXTURE_RANDSTR'})
        with self.assertRaises(m.Failure) as error:
            self.run_fixture(fixture_prefix() + [fixture_task(1), (200, {'code': 200}), fixture_task(1)])
        self.assertEqual(error.exception.stage, 'final_status')

    def test_submit_timeout_queries_state_without_repost(self):
        m = candidate()
        self.assertTrue(hasattr(m, 'Account'), 'account workflow missing')
        result, api, _ = self.run_fixture(fixture_prefix() + [fixture_task(1), m.RequestTimeout('network_timeout'), fixture_task(2)])
        self.assertEqual(result, 'signed')
        self.assertEqual(sum(c[0] == 'POST' and c[1].endswith('/tasks') for c in api.calls), 1)
        with self.assertRaises(m.Failure):
            self.run_fixture(fixture_prefix() + [fixture_task(1), m.RequestTimeout('network_timeout'), fixture_task(1)])

    def test_reward_commoncaptcha_gets_new_proof_only_after_pending_readback(self):
        m = candidate()
        api, cap = FixtureAPI(fixture_prefix() + [fixture_task(1), (200, {'code':10004}), fixture_task(1), (200, {'code':200}), fixture_task(2)]), FixtureCaptcha()
        try:
            status = m.Account(api, cap).run('FIXTURE_USER', 'FIXTURE_PASSWORD')
        except m.Failure as error:
            self.fail('common CAPTCHA rejection was not retried: ' + error.stage)
        self.assertEqual(status, 'signed')
        self.assertEqual(cap.calls, 2)
        posts = [i for i,c in enumerate(api.calls) if c[0]=='POST' and c[1].endswith('/tasks')]
        self.assertEqual(len(posts), 2)
        self.assertEqual(api.calls[posts[0]+1][0], 'GET')

    def test_login_commoncaptcha_uses_official_ticket_fields(self):
        result, api, captcha = self.run_fixture([(200, b'window.fea=0'), (200, {'code': 10004}), (200, {'code': 200}), (200, {'code': 200, 'data': 'FIXTURE_CSRF'}), fixture_task(2)])
        self.assertEqual(result, 'already_signed')
        self.assertEqual(captcha.calls, 1)
        self.assertEqual(api.calls[2][2], {'field': 'FIXTURE_USER', 'password': 'FIXTURE_PASSWORD', 'vticket': 'FIXTURE_TICKET', 'vrandstr': 'FIXTURE_RANDSTR'})

    def test_preload_changed_fails_before_password_post(self):
        m = candidate()
        self.assertTrue(hasattr(m, 'Account'), 'account workflow missing')
        for preload in [b'window.fea="0"', b'window.fea=1', b'window.fea=0;doSomething()']:
            api = FixtureAPI([(200, preload)])
            with self.assertRaises(m.Failure) as error:
                m.Account(api, FixtureCaptcha()).run('FIXTURE_USER', 'FIXTURE_PASSWORD')
            self.assertEqual(error.exception.stage, 'preload')
            self.assertEqual(len(api.calls), 1)


class TestWorkerRecipe(unittest.TestCase):
    def test_worker_image_has_no_browser_and_only_explicit_context(self):
        from pathlib import Path
        dockerfile = Path('Dockerfile.worker')
        self.assertTrue(dockerfile.exists(), 'minimal worker image recipe missing')
        text = dockerfile.read_text()
        for forbidden in ['playwright', 'chromium', 'COPY .', 'ADD ', 'curl ', 'wget ']:
            self.assertNotIn(forbidden, text.lower() if forbidden.islower() else text)
        self.assertIn('opencv-python-headless==', text)
        self.assertIn('numpy==', text)
        self.assertIn('USER 65534:65534', text)
        self.assertIn('/usr/local/bin/node', text)
        self.assertEqual(Path('.dockerignore').read_text().strip(), '*\n!Dockerfile.worker')


class TestSandbox(unittest.TestCase):
    def test_worker_commands_have_no_credentials_socket_or_sudo(self):
        m = candidate()
        self.assertTrue(hasattr(m, 'Worker'), 'sandbox worker missing')
        from pathlib import Path
        worker = m.Worker('signin-rainyun-http-worker:takeover')
        command = worker.command('node', [(Path('/fixture/tdc.js'), '/tdc.js'), (Path('/fixture/runner.js'), '/runner.js')], ['/runner.js'], 'fixture-container')
        self.assertEqual(command[0], 'docker')
        for flag in ['--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user=65534:65534', '--pids-limit=64', '--log-driver=none', '--pull=never']:
            self.assertIn(flag, command)
        self.assertNotIn('sudo', command)
        self.assertNotIn('/var/run/docker.sock', ' '.join(command))
        self.assertNotIn('--privileged', command)
        self.assertEqual(sum(item.startswith('type=bind,') for item in command), 2)

    def test_private_temporary_files_are_0600_and_cleaned(self):
        m = candidate()
        self.assertTrue(hasattr(m, 'PrivateWorkspace'), 'private temporary workspace missing')
        import stat
        with m.PrivateWorkspace() as directory:
            p = m.private_file(directory / 'fixture', b'FIXTURE_ONLY', worker_read=True)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            saved = directory
        self.assertFalse(saved.exists())

    def test_worker_timeout_removes_container_and_does_not_log_stderr(self):
        m = candidate()
        self.assertTrue(hasattr(m, 'Worker'), 'sandbox worker missing')
        import subprocess
        from unittest.mock import patch
        from test_worker_lifecycle import LifecycleCommands
        with m.PrivateWorkspace() as directory:
            commands = []
            lifecycle = LifecycleCommands()
            def run(command, **kwargs):
                commands.append((command, kwargs))
                if command[:2] == ['docker', 'run']:
                    raise subprocess.TimeoutExpired(command, 1, stderr=b'FIXTURE_SECRET')
                return lifecycle(command, **kwargs)
            with patch('rainyun_http.subprocess.run', side_effect=run), self.assertRaises(m.Failure) as error:
                m.Worker('fixture:image').run('node', [], [], directory)
            self.assertEqual(error.exception.stage, 'worker_timeout')
            removal = next(command for command, _ in commands if command[:2] == ['docker', 'rm'])
            self.assertIn('--force', removal)
            self.assertTrue(any(command[:3] == ['docker', 'container', 'ls'] for command, _ in commands))
            for _, kwargs in commands:
                self.assertIn('timeout', kwargs)
                self.assertEqual(kwargs['env'], {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root'})


def fixture_challenge(number=1):
    return {'sess': 'FIXTURE_SESS_' + str(number), 'data': {
        'dyn_show_info': {'bg_elem_cfg': {'size_2d': [300, 200], 'img_url': '/fixture-bg'},
                          'sprite_url': '/fixture-sprite',
                          'ins_elem_cfg': [{'size_2d': [90, 30], 'sprite_pos': [0, 0]}]},
        'comm_captcha_cfg': {'tdc_path': '/fixture-tdc', 'pow_cfg': {}}}}


class FixtureCaptchaTransport:
    def __init__(self, codes):
        self.codes, self.calls, self.count = list(codes), [], 0
    def request(self, method, url, payload=None, **kwargs):
        import json
        self.calls.append((method, url, payload))
        if 'prehandle?' in url:
            self.count += 1
            return 200, ('_aq_hermes(' + json.dumps(fixture_challenge(self.count)) + ');').encode()
        if 'new_verify' in url:
            code = self.codes.pop(0)
            return 200, {'errorCode': code, 'ticket': 'FIXTURE_TICKET' if code == '0' else '', 'randstr': 'FIXTURE_RANDSTR' if code == '0' else ''}
        return 200, b'PUBLIC_FIXTURE_ASSET_ONLY'


class FixtureWorker:
    def __init__(self):
        self.calls = []
    def run(self, entrypoint, mounts, args, directory):
        self.calls.append((entrypoint, mounts, args))
        if entrypoint == 'node':
            return {'collect': 'FIXTURE_COLLECT', 'tlg': 15, 'eks': 'FIXTURE_EKS', 'tkid': ''}
        return {'points': [[25, 30], [110, 70], [215, 140]]}


class TestCaptcha(unittest.TestCase):
    def build(self, codes):
        m = candidate()
        self.assertTrue(hasattr(m, 'Captcha'), 'bounded HTTP captcha implementation missing')
        t, w, pauses = FixtureCaptchaTransport(codes), FixtureWorker(), []
        return m.Captcha(w, transport=t, sleep=pauses.append), t, w, pauses

    def test_rejected_50_fetches_new_challenge_stops_on_success(self):
        cap, transport, worker, pauses = self.build(['50', '0', '50'])
        self.assertEqual(cap.solve('login'), {'ticket': 'FIXTURE_TICKET', 'randstr': 'FIXTURE_RANDSTR'})
        self.assertEqual(transport.count, 2)
        self.assertEqual(len(pauses), 1)
        self.assertGreaterEqual(pauses[0], 2)
        submits = [c[2] for c in transport.calls if 'new_verify' in c[1]]
        self.assertEqual([p['sess'] for p in submits], ['FIXTURE_SESS_1', 'FIXTURE_SESS_2'])
        import json
        self.assertEqual(json.loads(submits[1]['ans'])[0], {'elem_id': 1, 'type': 'DynAnswerType_POS', 'data': '25,30'})
        node_mounts = [c[1] for c in worker.calls if c[0] == 'node']
        self.assertTrue(all([dest for _, dest in mounts] == ['/tdc.js', '/runner.js'] for mounts in node_mounts))
        self.assertTrue(all('account' not in str(src) for c in worker.calls for src, _ in c[1]))

    def test_maximum_five_new_challenges_for_entire_account(self):
        m = candidate()
        cap, t, _, pauses = self.build(['50'] * 8)
        with self.assertRaises(m.Failure) as error:
            cap.solve('reward')
        self.assertEqual(error.exception.stage, 'captcha_exhausted')
        self.assertEqual(t.count, 5)
        self.assertEqual(len(pauses), 4)
        with self.assertRaises(m.Failure):
            cap.solve('login')
        self.assertEqual(t.count, 5)

    def test_429_stops_immediately_and_backs_off(self):
        m = candidate()
        cap, t, _, pauses = self.build(['429', '0'])
        with self.assertRaises(m.RateLimited):
            cap.solve('reward')
        self.assertEqual(t.count, 1)
        self.assertEqual(pauses, [60])

    def test_pow_has_bounded_search_and_matches_full_digest(self):
        m = candidate()
        self.assertTrue(hasattr(m, 'solve_pow'), 'bounded proof of work missing')
        import hashlib
        answer, elapsed = m.solve_pow({'prefix': 'FIXTURE_', 'md5': hashlib.md5(b'FIXTURE_7').hexdigest()}, limit=10)
        self.assertEqual(answer, 'FIXTURE_7')
        self.assertGreaterEqual(elapsed, 1)
        with self.assertRaises(m.Failure):
            m.solve_pow({'prefix': 'FIXTURE_', 'md5': hashlib.md5(b'FIXTURE_99').hexdigest()}, limit=10)

    def test_jsonp_is_parsed_not_executed(self):
        m = candidate()
        self.assertTrue(hasattr(m, 'parse_prehandle'), 'strict prehandle parser missing')
        import json
        value = m.parse_prehandle(('_aq_hermes(' + json.dumps(fixture_challenge()) + ');').encode())
        self.assertEqual(value['sess'], 'FIXTURE_SESS_1')
        for raw in [b'eval({});evil()', b'_aq_hermes({});evil()', b'{"sess":"FIXTURE"}']:
            with self.assertRaises(m.Failure):
                m.parse_prehandle(raw)


class TestCLI(unittest.TestCase):
    def test_env_parser_keeps_password_colons_and_never_expands_shell(self):
        m = candidate()
        self.assertTrue(hasattr(m, 'read_credentials'), 'safe env parser missing')
        with m.PrivateWorkspace() as directory:
            path = m.private_file(directory / 'a.env', b'# TEST FIXTURE\nusername=FIXTURE_USER\npassword="FIXTURE:$(false)=SECRET"\n')
            self.assertEqual(m.read_credentials(path), ('FIXTURE_USER', 'FIXTURE:$(false)=SECRET'))
            with self.assertRaises(m.Failure):
                m.read_credentials(m.private_file(directory / 'bad.env', b'password=FIXTURE_SECRET\n'))

    def test_multi_account_continues_failures_redacts_all_details(self):
        m = candidate()
        self.assertTrue(hasattr(m, 'run_accounts'), 'anonymous multi-account runner missing')
        import json
        class AccountFixture:
            def __init__(self, result): self.result = result
            def run(self, username, password, status_only):
                if self.result == 'raise': raise RuntimeError('FIXTURE_PASSWORD cookie=FIXTURE_COOKIE')
                return self.result
        outcomes = iter(['raise', 'already_signed', 'signed'])
        with m.PrivateWorkspace() as directory:
            paths = [m.private_file(directory / f'{n}.env',
                     f'username=FIXTURE_USER_{n}\npassword=FIXTURE_PASSWORD\n'.encode()) for n in range(3)]
            report, code = m.run_accounts(paths, m.DEFAULT_WORKER_IMAGE, account_factory=lambda: AccountFixture(next(outcomes)))
        self.assertEqual(code, 1)
        self.assertEqual((report['mode'], report['total'], report['success'], report['failed']), ('multi_account', 3, 2, 1))
        self.assertEqual([r['account'] for r in report['results']], [1, 2, 3])
        self.assertEqual([r['status'] for r in report['results']], ['error', 'already_done', 'signed'])
        self.assertEqual(report['results'][1]['task']['Status'], 2)
        self.assertEqual(report['results'][2]['task_after']['Status'], 2)
        serialized = json.dumps(report)
        for secret in ['FIXTURE_USER', 'FIXTURE_PASSWORD', 'FIXTURE_COOKIE', '.env', 'cookie', 'username']:
            self.assertNotIn(secret, serialized)
        self.assertEqual((report['transport'], report['browser_started'], report['model_api_used']), ('http', False, False))

    def test_duplicate_identity_cannot_restart_login_or_five_challenge_budget(self):
        m = candidate()
        import io, json
        from unittest.mock import patch
        for same_file in (False, True):
            with self.subTest(same_file=same_file), m.PrivateWorkspace() as directory:
                first = m.private_file(directory / 'first.env',
                    b'username=FIXTURE_DUPLICATE\npassword=FIXTURE_FIRST_PASSWORD\n')
                duplicate = first if same_file else m.private_file(directory / 'again.env',
                    b'field=FIXTURE_DUPLICATE\npassword=FIXTURE_OTHER_PASSWORD\n')
                other = m.private_file(directory / 'other.env',
                    b'username=FIXTURE_DISTINCT\npassword=FIXTURE_DISTINCT_PASSWORD\n')
                runs, created = [], []
                class BudgetAccount(m.Account):
                    def run(self, username, password, status_only):
                        replies = ([(200, b'window.fea=0')] + [(200, {'code': 10004})] * 6
                                   if username == 'FIXTURE_DUPLICATE' else
                                   fixture_prefix() + [fixture_task(2)])
                        self.api = FixtureAPI(replies)
                        runs.append((username, password, self.api, self.captcha))
                        return super().run(username, password, status_only)
                def factory():
                    captcha = m.Captcha(None, transport=object(), sleep=lambda seconds: None)
                    captcha.attempt = lambda purpose: {
                        'ticket': 'FIXTURE_TICKET', 'randstr': 'FIXTURE_RANDSTR'}
                    account = BudgetAccount(None, captcha)
                    created.append(account)
                    return account
                with patch('sys.stdout', new_callable=io.StringIO) as out:
                    report, code = m.run_accounts([first, duplicate, other], account_factory=factory)
                self.assertEqual(len(created), 2, 'duplicate allocated another account/captcha budget')
                self.assertEqual([run[:2] for run in runs], [
                    ('FIXTURE_DUPLICATE', 'FIXTURE_FIRST_PASSWORD'),
                    ('FIXTURE_DISTINCT', 'FIXTURE_DISTINCT_PASSWORD')])
                self.assertEqual(sum(run[3].attempts for run in runs), 5)
                self.assertEqual(sum(call[0] == 'POST' and call[1].endswith('/login')
                                     for call in runs[0][2].calls), 6)
                self.assertEqual((code, report['total'], report['success'], report['failed']), (1, 3, 1, 2))
                self.assertEqual(report['results'], [
                    {'account': 1, 'status': 'error', 'error_stage': 'login_captcha'},
                    {'account': 2, 'status': 'error', 'error_stage': 'credentials_duplicate'},
                    {'account': 3, 'status': 'already_done', 'task': {'Status': 2}}])
                self.assertEqual(out.getvalue(), '')
                for secret in ('FIXTURE_DUPLICATE', 'FIXTURE_FIRST_PASSWORD', 'FIXTURE_OTHER_PASSWORD',
                               'FIXTURE_DISTINCT', 'FIXTURE_DISTINCT_PASSWORD', '.env'):
                    self.assertNotIn(secret, json.dumps(report))

    def test_preflight_keeps_invalid_and_duplicate_configuration_rows_anonymous(self):
        m = candidate()
        import json
        from pathlib import Path
        from unittest.mock import patch
        events = []
        credentials = iter([m.Failure('credentials'), ('FIXTURE_A', 'FIXTURE_PASSWORD_A'),
                            ('FIXTURE_A', 'FIXTURE_CHANGED_PASSWORD'), ('FIXTURE_B', 'FIXTURE_PASSWORD_B')])
        def reader(path):
            events.append('read')
            result = next(credentials)
            if isinstance(result, Exception):
                raise result
            return result
        class AccountFixture:
            def __init__(self):
                events.append('account')
            def run(self, username, password, status_only):
                events.append('login')
                return 'already_signed'
        with patch.object(m, 'read_credentials', side_effect=reader) as reads:
            report, code = m.run_accounts([Path('/unused') / str(n) for n in range(4)],
                                         account_factory=AccountFixture)
        self.assertEqual(events, ['read'] * 4 + ['account', 'login'] * 2)
        self.assertEqual(reads.call_count, 4)
        self.assertEqual((code, report['total'], report['success'], report['failed']), (1, 4, 2, 2))
        self.assertEqual(report['results'], [
            {'account': 1, 'status': 'error', 'error_stage': 'credentials'},
            {'account': 2, 'status': 'already_done', 'task': {'Status': 2}},
            {'account': 3, 'status': 'error', 'error_stage': 'credentials_duplicate'},
            {'account': 4, 'status': 'already_done', 'task': {'Status': 2}}])
        for secret in ('FIXTURE_', '/unused', 'username', 'password'):
            self.assertNotIn(secret, json.dumps(report))

    def test_exact_identity_check_does_not_normalize_api_credentials(self):
        m = candidate()
        from pathlib import Path
        from unittest.mock import patch
        credentials = [('FIXTURE_User', ' FIXTURE_PASSWORD_A '),
                       ('fixture_user', 'FIXTURE_PASSWORD_B'),
                       (' FIXTURE_User ', 'FIXTURE_PASSWORD_C')]
        calls = []
        class AccountFixture:
            def run(self, username, password, status_only):
                calls.append((username, password, status_only))
                return 'already_signed'
        with patch.object(m, 'read_credentials', side_effect=credentials):
            report, code = m.run_accounts([Path('/unused') / str(n) for n in range(3)],
                                         account_factory=AccountFixture, status_only=True)
        self.assertEqual(calls, [(username, password, True) for username, password in credentials])
        self.assertEqual((code, report['total'], report['success'], report['failed']), (0, 3, 3, 0))

    def test_main_defaults_to_all_accounts_and_accepts_single_env(self):
        m = candidate()
        self.assertTrue(hasattr(m, 'main'), 'CLI entrypoint missing')
        from unittest.mock import patch
        import io,json
        with m.PrivateWorkspace() as directory:
            for n in ['b.env','a.env']:
                m.private_file(directory / n, b'field=FIXTURE_USER\npassword=FIXTURE_PASSWORD\n')
            m.private_file(directory / 'ignored.txt', b'NOT_AN_ACCOUNT')
            m.private_file(directory / '.hidden.env', b'NOT_AN_ACCOUNT')
            (directory / 'link.env').symlink_to(directory / 'a.env')
            def run(paths, worker_image, status_only):
                self.assertEqual([p.name for p in paths], ['a.env', 'b.env'])
                self.assertEqual(worker_image, 'fixture:image')
                self.assertTrue(status_only)
                return {'mode':'multi_account','total':2,'success':2,'failed':0,'results':[]}, 0
            with patch('rainyun_http.os.geteuid', return_value=0), patch('rainyun_http.run_accounts', side_effect=run), patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(m.main(['--env-dir', str(directory), '--worker-image', 'fixture:image', '--status-only']), 0)
                self.assertEqual(json.loads(out.getvalue())['total'], 2)
            with patch('rainyun_http.os.geteuid', return_value=0), patch('rainyun_http.run_accounts', return_value=({'fixture': True},0)) as mocked, patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(m.main(['--env', str(directory / 'a.env')]), 0)
                self.assertEqual(mocked.call_args.args[0], [directory / 'a.env'])


class TestHardening(unittest.TestCase):
    def test_captcha_rate_limit_is_not_backed_off_twice(self):
        m = candidate()
        cap, _, _, pauses = TestCaptcha().build(['429'])
        class AccountFixture:
            def run(self, *args): return cap.solve('login')
        with m.PrivateWorkspace() as directory:
            path = m.private_file(directory / 'fixture.env', b'username=FIXTURE_USER\npassword=FIXTURE_PASSWORD\n')
            report, code = m.run_accounts([path], account_factory=AccountFixture, sleep=pauses.append)
        self.assertEqual((code, report['results'][0]['error_stage']), (1, 'rate_limit'))
        self.assertEqual(pauses, [60])

    def test_api_429_body_stops_login_without_captcha(self):
        m = candidate()
        api, cap = FixtureAPI([(200, b'window.fea=0'), (200, {'code': 429})]), FixtureCaptcha()
        with self.assertRaises(m.Failure) as error:
            m.Account(api, cap).run('FIXTURE_USER', 'FIXTURE_PASSWORD')
        self.assertIsInstance(error.exception, m.RateLimited)
        self.assertEqual(cap.calls, 0)

    def test_task_numeric_float_is_not_a_verified_status(self):
        m = candidate()
        with self.assertRaises(m.Failure):
            m.Account(FixtureAPI(fixture_prefix() + [fixture_task(2.0)]), FixtureCaptcha()).run('FIXTURE_USER', 'FIXTURE_PASSWORD')

    def test_daily_requires_original_literal_int_status(self):
        m = candidate()
        for status in (1, 2):
            with self.subTest(valid=status):
                actual = m.Account(FixtureAPI([fixture_task(status)]), FixtureCaptcha()).daily()
                self.assertIs(type(actual), int)
                self.assertEqual(actual, status)
        for status in ('1', '2', True, False, 1.0, 2.0, 0, 3, None):
            with self.subTest(invalid=status, value_type=type(status).__name__):
                response = fixture_task(status)
                response[1]['message'] = 'FIXTURE_PRIVATE_RESPONSE'
                with self.assertRaises(m.Failure) as error:
                    m.Account(FixtureAPI([response]), FixtureCaptcha()).daily()
                self.assertEqual(error.exception.stage, 'task_status')
                self.assertEqual(str(error.exception), 'task_status')

    def test_string_status_cannot_forge_success_in_anonymous_report(self):
        m = candidate()
        import io, json
        from pathlib import Path
        from unittest.mock import patch
        cases = [
            (fixture_prefix() + [fixture_task('1')], 0),
            (fixture_prefix() + [fixture_task('2')], 0),
            (fixture_prefix() + [fixture_task(1), (200, {'code': 200}), fixture_task('2')], 1),
            (fixture_prefix() + [fixture_task(1), m.RequestTimeout('network_timeout'), fixture_task('2')], 1),
        ]
        for replies, expected_posts in cases:
            with self.subTest(expected_posts=expected_posts, replies=replies):
                replies[-1][1]['message'] = 'FIXTURE_PRIVATE_RESPONSE'
                api, captcha = FixtureAPI(replies), FixtureCaptcha()
                with patch.object(m, 'read_credentials', return_value=('FIXTURE_USER', 'FIXTURE_PASSWORD')), \
                        patch('sys.stdout', new_callable=io.StringIO) as out:
                    report, code = m.run_accounts([Path('/unused/fixture')],
                        account_factory=lambda: m.Account(api, captcha))
                self.assertEqual((code, report['total'], report['success'], report['failed']), (1, 1, 0, 1))
                self.assertEqual(report['results'], [
                    {'account': 1, 'status': 'error', 'error_stage': 'task_status'}])
                self.assertEqual(sum(call[0] == 'POST' and call[1].endswith('/tasks')
                                     for call in api.calls), expected_posts)
                self.assertEqual(captcha.calls, expected_posts)
                self.assertEqual(out.getvalue(), '')
                for secret in ('FIXTURE_USER', 'FIXTURE_PASSWORD', 'FIXTURE_PRIVATE_RESPONSE'):
                    self.assertNotIn(secret, json.dumps(report))

    def test_worker_host_output_is_resource_limited(self):
        m = candidate()
        import subprocess
        from unittest.mock import patch
        from test_worker_lifecycle import LifecycleCommands
        called = []
        lifecycle = LifecycleCommands()
        def run(command, **kwargs):
            called.append((command, kwargs))
            if command[:2] == ['docker', 'run']:
                self.assertIn('preexec_fn', kwargs, 'host Docker output file is not size-limited')
                self.assertTrue(kwargs.get('close_fds'))
            return lifecycle(command, **kwargs)
        with m.PrivateWorkspace() as directory, patch('rainyun_http.subprocess.run', side_effect=run):
            self.assertEqual(m.Worker().run('node', [], [], directory), {})


if __name__ == '__main__':
    unittest.main()
