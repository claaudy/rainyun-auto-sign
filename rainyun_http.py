#!/usr/bin/env python3
"""Rainyun host HTTP client. Standard library only; no browser/model runtime."""
import time
import gzip
import http.cookiejar
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent
DEFAULT_WORKER_IMAGE = 'signin-rainyun-http-worker:takeover'
DOCKER_ENV = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root'}


@contextmanager
def PrivateWorkspace():
    base = ROOT / '.runtime'
    base.mkdir(mode=0o700, exist_ok=True)
    os.chmod(base, 0o700)
    with tempfile.TemporaryDirectory(prefix='captcha-', dir=base) as name:
        yield Path(name)


def private_file(path, data, worker_read=False):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
    if worker_read:
        # A 0600 bind file must be owned by the sandbox UID to be readable.
        uid = 65534 if os.geteuid() == 0 else os.geteuid()
        os.chown(path, uid, uid if os.geteuid() == 0 else os.getegid())
    return path


def limit_worker_output():
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (2_000_000, 2_000_000))


class WorkerCleanup:
    """Root system-manager reaper; survives loss of the entire caller process group."""
    def __init__(self, name):
        if not re.fullmatch(r'rainyun-isolated-[0-9a-f]{32}', name):
            raise CriticalFailure('worker_reaper')
        self.name = name
        self.unit = 'rainyun-worker-cleanup-' + name.removeprefix('rainyun-isolated-')

    def register(self):
        command = ['/usr/bin/systemd-run', '--system', '--quiet', '--collect',
                   '--unit=' + self.unit, '--on-active=40s',
                   '--timer-property=AccuracySec=1s',
                   '--timer-property=RemainAfterElapse=no',
                   '--property=Type=oneshot', '--property=User=root',
                   '--property=Group=root', '--property=TimeoutStartSec=15s',
                   '--property=TimeoutStopSec=5s', '--property=KillMode=control-group',
                   '/usr/bin/docker', 'rm', '--force', self.name]
        try:
            registered = subprocess.run(command, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, env=DOCKER_ENV, check=False)
            if registered.returncode:
                raise CriticalFailure('worker_reaper')
            timer = subprocess.run(['/usr/bin/systemctl', '--system', 'show',
                self.unit + '.timer', '--property=LoadState,ActiveState,Triggers'],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, timeout=5, env=DOCKER_ENV, check=False)
            properties = dict(line.split('=', 1) for line in timer.stdout.splitlines() if '=' in line)
            if (timer.returncode or properties.get('LoadState') != 'loaded' or
                    properties.get('ActiveState') != 'active' or
                    properties.get('Triggers') != self.unit + '.service'):
                raise CriticalFailure('worker_reaper')
        except (OSError, subprocess.TimeoutExpired):
            # A registration timeout may still have armed the timer. Never disarm
            # it on uncertainty, and never start Docker without confirmation.
            raise CriticalFailure('worker_reaper') from None

    def remove_and_confirm(self):
        try:
            removed = subprocess.run(['docker', 'rm', '--force', self.name],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, timeout=10, env=DOCKER_ENV, check=False)
            containers = subprocess.run(['docker', 'container', 'ls', '--all', '--filter',
                'name=^/' + self.name + '$', '--format', '{{.ID}}'],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=10, env=DOCKER_ENV, check=False)
            # --rm can win the race. Only the exact, named "already absent" error
            # is acceptable; never reinterpret an unknown Docker error as success.
            already_absent = (removed.returncode == 1 and removed.stderr ==
                'Error response from daemon: No such container: ' + self.name + '\n')
            if ((removed.returncode and not already_absent) or containers.returncode or
                    not isinstance(containers.stdout, str) or containers.stdout.strip()):
                raise CriticalFailure('worker_cleanup')
        except (OSError, subprocess.TimeoutExpired):
            # Do not cancel PID1's cleanup on timeout, daemon failure, or uncertainty.
            raise CriticalFailure('worker_cleanup') from None

    def cancel(self):
        # Called ONLY after a successful exact-name Docker absence confirmation.
        units = [self.unit + '.timer', self.unit + '.service']
        try:
            subprocess.run(['/usr/bin/systemctl', '--system', 'stop'] + units,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, env=DOCKER_ENV, check=False)
            # --collect units can disappear during stop. A nonzero stop status
            # is not evidence of a live reaper; require a successful manager read.
            # list-units does not try to load absent units, unlike show.
            state = subprocess.run(['/usr/bin/systemctl', '--system', 'list-units',
                '--all', '--no-pager', '--output=json'] + units,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, timeout=5, env=DOCKER_ENV, check=False)
            try:
                observed = json.loads(state.stdout)
            except (TypeError, ValueError):
                raise CriticalFailure('worker_reaper') from None
            if (state.returncode or not isinstance(observed, list) or any(
                    not isinstance(item, dict) or item.get('unit') not in units
                    or item.get('load') != 'loaded' or item.get('active') != 'inactive'
                    for item in observed)):
                raise CriticalFailure('worker_reaper')
        except (OSError, subprocess.TimeoutExpired):
            raise CriticalFailure('worker_reaper') from None


class Worker:
    def __init__(self, image=DEFAULT_WORKER_IMAGE, timeout=25):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:@-]{0,250}', image):
            raise Failure('worker_image')
        self.image, self.timeout = image, timeout

    def command(self, entrypoint, mounts, args, name):
        command = ['docker', 'run', '--rm', '--name', name, '--pull=never',
                   '--label=hermes.signin.site=rainyun',
                   '--network=none', '--read-only', '--cap-drop=ALL',
                   '--security-opt=no-new-privileges', '--user=65534:65534',
                   '--memory=256m', '--memory-swap=256m', '--cpus=1',
                   '--pids-limit=64', '--ulimit=nofile=64:64',
                   '--ulimit=fsize=2048:2048', '--log-driver=none',
                   '--tmpfs=/tmp:rw,noexec,nosuid,size=8m,mode=1777',
                   '--entrypoint', entrypoint]
        for source, destination in mounts:
            if ',' in str(source) or not source.is_absolute():
                raise Failure('worker_mount')
            command.extend(['--mount', f'type=bind,src={source},dst={destination},readonly'])
        return command + [self.image] + args

    def run(self, entrypoint, mounts, args, directory):
        name = 'rainyun-isolated-' + uuid.uuid4().hex
        command = self.command(entrypoint, mounts, args, name)
        cleanup = WorkerCleanup(name)
        cleanup.register()  # PID1 must own a confirmed reaper before Docker starts.
        output = private_file(directory / (name + '.out'), b'')
        try:
            with output.open('wb') as stream:
                process = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stream,
                    stderr=subprocess.DEVNULL, timeout=self.timeout, env=DOCKER_ENV, check=False,
                    close_fds=True, preexec_fn=limit_worker_output)
            if process.returncode or output.stat().st_size > 1_000_000:
                raise Failure('worker_output')
            try:
                value = json.loads(output.read_bytes())
            except (ValueError, OSError):
                raise Failure('worker_output') from None
            if not isinstance(value, dict):
                raise Failure('worker_output')
            return value
        except subprocess.TimeoutExpired:
            raise Failure('worker_timeout') from None
        except OSError:
            raise Failure('worker_start') from None
        finally:
            try:
                cleanup.remove_and_confirm()
                cleanup.cancel()
            finally:
                try:
                    output.unlink(missing_ok=True)
                except OSError:
                    raise CriticalFailure('worker_cleanup') from None

APP = 'https://app.rainyun.com'
API = 'https://api.v2.rainyun.com'
CAPTCHA = 'https://turing.captcha.qcloud.com'
GTIMG = 'https://turing.captcha.gtimg.com'
UA = ('Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36')


class Failure(Exception):
    """Only constant stage strings may be exposed by the CLI."""
    def __init__(self, stage):
        self.stage = stage
        super().__init__(stage)


class CriticalFailure(Failure):
    """Lifecycle uncertainty: retain the independent reaper and abort the batch."""


class RateLimited(Failure):
    pass


class RequestTimeout(Failure):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # No redirects at all: never forward passwords/session/CSRF/proofs.
        raise Failure('redirect')


def validate_url(url, origins):
    try:
        p = urllib.parse.urlsplit(url)
        origin = f'{p.scheme}://{p.netloc}'
        if (origin not in origins or p.username or p.password or p.fragment
                or p.scheme != 'https' or '\\' in url or any(ord(c) < 33 for c in url)):
            raise ValueError()
    except ValueError:
        raise Failure('origin') from None
    return p


class Transport:
    def __init__(self, kind, timeout=30):
        self.kind, self.timeout = kind, timeout
        self.origins = (APP, API) if kind == 'api' else (CAPTCHA, GTIMG)
        self.cookies = http.cookiejar.CookieJar()
        # Explicitly ignore environment HTTP(S)_PROXY settings.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            NoRedirect(), urllib.request.HTTPCookieProcessor(self.cookies))
        self.csrf = 'undefined'

    def request(self, method, url, payload=None, raw=False, form=False):
        p = validate_url(url, self.origins)
        headers = {'User-Agent': UA, 'Referer': APP + '/' if self.kind == 'api' else GTIMG + '/'}
        if p.netloc == 'api.v2.rainyun.com':
            headers.update({'RYS': rys_header(), 'X-CSRF-Token': self.csrf, 'Origin': APP})
        elif self.kind != 'api':
            headers['Origin'] = GTIMG
        data = None
        if payload is not None:
            data = (urllib.parse.urlencode(payload) if form else json.dumps(payload)).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8' if form else 'application/json'
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                response = e
            with response:
                status = response.status
                body = response.read(8_000_001)
            if status == 429:
                raise RateLimited('rate_limit')
            if 300 <= status < 400:
                raise Failure('redirect')
            if len(body) > 8_000_000:
                raise Failure('response_size')
            if body.startswith(b'\x1f\x8b'):
                import io
                with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                    body = stream.read(8_000_001)
                if len(body) > 8_000_000:
                    raise Failure('response_size')
            return status, body if raw else json.loads(body)
        except (socket.timeout, TimeoutError):
            raise RequestTimeout('network_timeout') from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise RequestTimeout('network_timeout') from None
            raise Failure('network') from None
        except (ValueError, OSError, EOFError):
            raise Failure('response') from None

    def has_session(self):
        return any(c.name == 'rain-session' and bool(c.value) and
                   c.domain.lstrip('.') in ('rainyun.com', 'api.v2.rainyun.com') for c in self.cookies)

    def install_csrf(self, value):
        self.csrf = value
        self.cookies.set_cookie(http.cookiejar.Cookie(
            0, 'X-CSRF-Token', urllib.parse.quote(value, safe=''), None, False,
            '.rainyun.com', True, True, '/', True, True, None, True, None, None, {}))


def rys_header(milliseconds=None):
    if milliseconds is None:
        milliseconds = time.time_ns() // 1_000_000
    seconds, fraction = divmod(milliseconds, 1000)
    return str(seconds) if not fraction else f'{seconds}.{fraction:03d}'.rstrip('0')


class RetryChallenge(Failure):
    pass


def parse_prehandle(raw):
    try:
        match = re.fullmatch(rb'\s*_aq_hermes\((.*)\)\s*;?\s*', raw, re.S)
        if not match:
            raise ValueError()
        value = json.loads(match.group(1))
        if not isinstance(value, dict) or not isinstance(value.get('sess'), str) or not value['sess']:
            raise ValueError()
        if not isinstance(value.get('data'), dict):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise Failure('captcha_prehandle') from None


def solve_pow(config, limit=2_000_000):
    import hashlib
    prefix, target = config.get('prefix'), config.get('md5')
    if not isinstance(prefix, str) or len(prefix) > 256 or not isinstance(target, str) or not re.fullmatch('[a-fA-F0-9]{32}', target):
        raise Failure('captcha_pow_config')
    start = time.monotonic()
    for number in range(limit):
        candidate = prefix + str(number)
        if hashlib.md5(candidate.encode()).hexdigest() == target.lower():
            return candidate, max(1, round((time.monotonic() - start) * 1000))
        if number % 1024 == 0 and time.monotonic() - start > 5:
            break
    raise Failure('captcha_pow_budget')


class Captcha:
    """One total five-new-challenge budget per account (login + reward)."""
    def __init__(self, worker, transport=None, sleep=time.sleep):
        self.worker = worker
        self.transport = transport if transport is not None else Transport('captcha')
        self.sleep, self.attempts = sleep, 0

    def solve(self, purpose):
        while self.attempts < 5:
            if self.attempts:
                self.sleep(2)
            self.attempts += 1
            try:
                return self.attempt(purpose)
            except RateLimited as error:
                self.sleep(60)
                error.backed_off = True
                raise
            except RetryChallenge:
                continue
        raise Failure('captcha_exhausted')

    def asset(self, relative):
        if not isinstance(relative, str):
            raise Failure('captcha_asset')
        url = urllib.parse.urljoin(CAPTCHA, relative)
        validate_url(url, (CAPTCHA, GTIMG))
        status, raw = self.transport.request('GET', url, raw=True)
        if status != 200 or not raw:
            raise Failure('captcha_asset')
        return raw

    def attempt(self, purpose):
        import base64
        params = {'aid': '2039519451', 'protocol': 'https', 'accver': '1', 'showtype': 'embed',
                  'ua': base64.b64encode(UA.encode()).decode(), 'noheader': '1', 'fb': '1', 'aged': '0',
                  'enableAged': '0', 'enableDarkMode': '0', 'grayscale': '1', 'clientype': '2',
                  'lang': 'zh-cn', 'entry_url': APP + ('/auth/login' if purpose == 'login' else '/account/reward/earn'),
                  'callback': '_aq_hermes'}
        status, raw = self.transport.request('GET', CAPTCHA + '/cap_union_prehandle?' + urllib.parse.urlencode(params), raw=True)
        if status != 200:
            raise Failure('captcha_prehandle')
        challenge = parse_prehandle(raw)
        try:
            info = challenge['data']['dyn_show_info']
            comm = challenge['data']['comm_captcha_cfg']
            bg = info['bg_elem_cfg']
            dimensions = bg['size_2d']
            if (not isinstance(dimensions, list) or len(dimensions) != 2 or
                    any(type(n) is not int or n < 1 or n > 4096 for n in dimensions)):
                raise ValueError()
            instructions = info['ins_elem_cfg']
            if not isinstance(instructions, list) or len(instructions) not in (1, 3):
                raise ValueError()
            # Only public image layout goes to CV worker. No sess/proofs/config.
            layout = {'bg_elem_cfg': {'size_2d': dimensions}, 'ins_elem_cfg': [
                {'size_2d': c['size_2d'], 'sprite_pos': c['sprite_pos']} for c in instructions]}
            background = self.asset(bg['img_url'])
            sprite = self.asset(info['sprite_url'])
            tdc_code = self.asset(comm['tdc_path'])
        except (KeyError, TypeError, ValueError):
            raise Failure('captcha_schema') from None
        with PrivateWorkspace() as directory:
            paths = {}
            for name, data in [('background.png', background), ('sprite.png', sprite),
                    ('layout.json', json.dumps(layout).encode()), ('tdc.js', tdc_code),
                    ('runner.js', (ROOT / 'tdc_runner.js').read_bytes()),
                    ('matcher.py', (ROOT / 'tenvision_raw.py').read_bytes())]:
                paths[name] = private_file(directory / name, data, worker_read=True)
            try:
                solved = self.worker.run('python3', [(paths['background.png'], '/background.png'),
                    (paths['sprite.png'], '/sprite.png'), (paths['layout.json'], '/layout.json'),
                    (paths['matcher.py'], '/matcher.py')], ['/matcher.py'], directory)
                points = solved.get('points')
                if not isinstance(points, list) or len(points) != 3:
                    raise ValueError()
                for point in points:
                    if (not isinstance(point, list) or len(point) != 2 or
                            any(type(n) is not int for n in point) or
                            not (0 <= point[0] < dimensions[0] and 0 <= point[1] < dimensions[1])):
                        raise ValueError()
                if len({tuple(p) for p in points}) != 3:
                    raise ValueError()
            except (ValueError, TypeError):
                raise RetryChallenge('captcha_coordinates') from None
            except Failure as error:
                if error.stage in ('worker_output', 'worker_timeout'):
                    raise RetryChallenge('captcha_recognition') from None
                raise
            try:
                proof = self.worker.run('node', [(paths['tdc.js'], '/tdc.js'),
                    (paths['runner.js'], '/runner.js')], ['/runner.js'], directory)
            except Failure as error:
                if error.stage in ('worker_output', 'worker_timeout'):
                    raise RetryChallenge('captcha_tdc') from None
                raise
        collect, eks = proof.get('collect'), proof.get('eks')
        if (not isinstance(collect, str) or not collect or len(collect) > 300000 or
                not isinstance(eks, str) or not eks or len(eks) > 300000 or
                proof.get('tlg') != len(collect.encode('utf-16-le')) // 2):
            raise Failure('captcha_proof')
        payload = {'collect': collect, 'tlg': proof['tlg'], 'eks': eks, 'sess': challenge['sess'],
            'ans': json.dumps([{'elem_id': i+1, 'type': 'DynAnswerType_POS', 'data': f'{p[0]},{p[1]}'}
                               for i, p in enumerate(points)], separators=(',', ':'))}
        if proof.get('tkid'):
            if not isinstance(proof['tkid'], str) or len(proof['tkid']) > 10000:
                raise Failure('captcha_proof')
            payload['tkid'] = proof['tkid']
        pow_config = comm.get('pow_cfg') or {}
        if pow_config:
            payload['pow_answer'], payload['pow_calc_time'] = solve_pow(pow_config)
        status, response = self.transport.request('POST', CAPTCHA + '/cap_union_new_verify', payload, form=True)
        if status != 200 or not isinstance(response, dict):
            raise Failure('captcha_verify')
        code = str(response.get('errorCode'))
        if code == '429':
            raise RateLimited('rate_limit')
        if code == '50':
            raise RetryChallenge('captcha_rejected_50')
        if code != '0':
            raise Failure('captcha_verify')
        if any(not isinstance(response.get(key), str) or not response[key] or len(response[key]) > 10000
               for key in ('ticket', 'randstr')):
            raise Failure('captcha_ticket')
        return {key: response[key] for key in ('ticket', 'randstr')}


class Account:
    def __init__(self, api, captcha):
        self.api, self.captcha = api, captcha

    def api_call(self, method, path, payload=None):
        status, data = self.api.request(method, API + path, payload)
        if status != 200 or not isinstance(data, dict):
            raise Failure('api_http')
        # Only explicit 429 is classified as rate limiting, never 10004.
        if data.get('code') in (429, '429'):
            raise RateLimited('rate_limit')
        return data

    def daily(self):
        data = self.api_call('GET', '/user/reward/tasks')
        if data.get('code') != 200 or not isinstance(data.get('data'), list):
            raise Failure('tasks')
        tasks = [t for t in data['data'] if isinstance(t, dict) and t.get('Name') == '每日签到']
        if (len(tasks) != 1 or type(tasks[0].get('Status')) is not int
                or tasks[0]['Status'] not in (1, 2)):
            raise Failure('task_status')
        return tasks[0]['Status']

    def run(self, username, password, status_only=False):
        import re
        status, raw = self.api.request('GET', APP + '/preload.js', raw=True)
        if status != 200 or not re.fullmatch(rb'\s*window\.fea\s*=\s*0\s*;?\s*', raw):
            raise Failure('preload')
        payload = {'field': username, 'password': password}
        login = self.api_call('POST', '/user/login', payload)
        # Current public frontend: CommonCaptchaError -> same callback(ticket,randstr).
        # Hard cap also protects a malformed/injected captcha implementation.
        for _ in range(5):
            if login.get('code') != 10004:
                break
            proof = self.captcha.solve('login')
            payload.update({'vticket': proof['ticket'], 'vrandstr': proof['randstr']})
            login = self.api_call('POST', '/user/login', payload)
        if login.get('code') != 200 or not self.api.has_session():
            raise Failure('login_captcha' if login.get('code') == 10004 else 'login')
        csrf = self.api_call('GET', '/user/csrf')
        token = csrf.get('data')
        if csrf.get('code') != 200 or not isinstance(token, str) or not token:
            raise Failure('csrf')
        self.api.install_csrf(token)
        before = self.daily()
        if before == 2:
            return 'already_signed'
        if status_only:
            return 'pending'
        for _ in range(5):
            proof = self.captcha.solve('reward')
            payload = {'task_name': '每日签到', 'verifyCode': '',
                       'vticket': proof['ticket'], 'vrandstr': proof['randstr']}
            timed_out = False
            try:
                result = self.api_call('POST', '/user/reward/tasks', payload)
            except RequestTimeout:
                timed_out = True
                result = None
            # A timeout is ambiguous: read back, never retry that POST blindly.
            after = self.daily()
            if timed_out:
                if after == 2:
                    return 'signed'
                raise Failure('submit_timeout')
            if result.get('code') == 10004 and after == 1:
                # Explicit rejection + verified pending state permits a fresh proof.
                # Captcha owns the shared five-challenge budget and retry spacing.
                continue
            if after != 2:
                raise Failure('final_status')
            if result.get('code') != 200:
                raise Failure('submit')
            return 'signed'
        raise Failure('submit')


def read_credentials(path):
    import stat
    fields = {}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, encoding='utf-8') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise Failure('credentials')
            text = stream.read(65537)
        if len(text) > 65536:
            raise Failure('credentials')
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].lstrip()
            match = re.fullmatch(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*[:=]\s*(.*)', line)
            if not match:
                raise Failure('credentials')
            key, value = match.groups()
            if len(value) >= 2 and value[0] in ('\"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            if key in fields:
                raise Failure('credentials')
            fields[key] = value
        username = fields.get('username') or fields.get('field')
        password = fields.get('password')
        if not username or not password:
            raise Failure('credentials')
        return username, password
    except (OSError, UnicodeError):
        raise Failure('credentials') from None


SAFE_STAGES = frozenset('''origin redirect response_size response network network_timeout
    rate_limit worker_image worker_mount worker_timeout worker_start worker_output
    worker_reaper worker_cleanup
    captcha_prehandle captcha_pow_config captcha_pow_budget captcha_exhausted captcha_asset
    captcha_schema captcha_proof captcha_verify captcha_ticket captcha_coordinates
    captcha_recognition captcha_tdc captcha_rejected_50 api_http tasks task_status preload
    login login_captcha csrf submit_timeout final_status submit credentials credentials_duplicate accounts
    root_required arguments status_pending internal'''.split())


def blank_report():
    return {'mode': 'multi_account', 'transport': 'http', 'browser_started': False,
            'model_api_used': False, 'total': 0, 'success': 0, 'failed': 0, 'results': []}


def run_accounts(files, worker_image=DEFAULT_WORKER_IMAGE, status_only=False,
                 account_factory=None, sleep=time.sleep):
    report = blank_report()
    # Preflight every file before any login; compare exact, unmodified usernames.
    # Reserve even failed accounts' identities so no later file gets a new budget.
    prepared, seen = [], set()
    for path in files:
        try:
            username, password = read_credentials(path)
            if username in seen:
                raise Failure('credentials_duplicate')
            seen.add(username)
            prepared.append((username, password))
        except Exception as error:
            prepared.append(error)
    for index, credentials in enumerate(prepared, 1):
        row = {'account': index}
        try:
            if isinstance(credentials, Exception):
                raise credentials
            username, password = credentials
            account = (account_factory() if account_factory is not None else
                       Account(Transport('api'), Captcha(Worker(worker_image))))
            status = account.run(username, password, status_only)
            if status == 'already_signed':
                row.update(status='already_done', task={'Status': 2})
            elif status == 'signed':
                row.update(status='signed', task_after={'Status': 2})
            elif status == 'pending':
                row.update(status='error', error_stage='status_pending', task={'Status': 1})
            else:
                raise Failure('internal')
        except CriticalFailure as error:
            row.update(status='error', error_stage=error.stage if error.stage in SAFE_STAGES else 'internal')
            report['aborted'] = True
            report['results'].append(row)
            break  # No next account, challenge, or worker after lifecycle uncertainty.
        except Failure as error:
            row.update(status='error', error_stage=error.stage if error.stage in SAFE_STAGES else 'internal')
            if isinstance(error, RateLimited) and not getattr(error, 'backed_off', False):
                sleep(60)
        except Exception:
            # Exception text can contain the HTTP body, credentials or proof values.
            row.update(status='error', error_stage='internal')
        report['results'].append(row)
    report['total'] = len(report['results'])
    report['failed'] = sum(row['status'] == 'error' for row in report['results'])
    report['success'] = report['total'] - report['failed']
    return report, int(bool(report['failed']) or not report['total'])


def main(argv=None):
    import argparse
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise Failure('arguments')
    parser = Parser(description='Host-root Rainyun HTTP-only CLI; no browser or model API.')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--env', type=Path, help='One account file (no credentials in arguments)')
    group.add_argument('--env-dir', type=Path, default=ROOT / 'accounts', help='All nonhidden *.env files; default ./accounts next to script')
    parser.add_argument('--worker-image', default=DEFAULT_WORKER_IMAGE)
    parser.add_argument('--status-only', action='store_true', help='Read status only; pending returns exit 1; login may require CAPTCHA')
    try:
        args = parser.parse_args(argv)
        if os.geteuid() != 0:
            raise Failure('root_required')
        Worker(args.worker_image)  # Validate even if all accounts are already signed.
        if args.env is not None:
            paths = [args.env.absolute()]
        else:
            directory = args.env_dir.absolute()
            paths = sorted(p for p in directory.iterdir()
                           if p.suffix == '.env' and not p.name.startswith('.') and not p.is_symlink() and p.is_file())
        if not paths:
            raise Failure('accounts')
        report, code = run_accounts(paths, args.worker_image, args.status_only)
    except Failure as error:
        report, code = blank_report(), 1
        report['error_stage'] = error.stage if error.stage in SAFE_STAGES else 'internal'
    except OSError:
        report, code = blank_report(), 1
        report['error_stage'] = 'accounts'
    except Exception:
        report, code = blank_report(), 1
        report['error_stage'] = 'internal'
    print(json.dumps(report, ensure_ascii=False, separators=(',', ':')), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
