"""Single-browser pairing. Credentials stay in private local state and host-only cookies."""
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import urlsplit

COOKIE = '__Host-phonemic'
SESSION_SECONDS = 90 * 24 * 60 * 60
PAIR_SECONDS = 5 * 60


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_origin(value):
    try:
        p = urlsplit(value)
        if p.username or p.password or p.query or p.fragment or p.path not in ('', '/'):
            return None
        if p.scheme not in ('https', 'http') or not p.hostname:
            return None
        if p.scheme == 'http' and p.hostname not in ('localhost', '127.0.0.1', '::1'):
            return None
        port = p.port
        host = p.hostname.lower()
        if ':' in host:
            host = '[' + host + ']'
        if port and port != (443 if p.scheme == 'https' else 80):
            host += ':' + str(port)
        return p.scheme + '://' + host
    except (ValueError, TypeError):
        return None


class AuthStore:
    def __init__(self, path=None, clock=time.time):
        self.path = Path(path or os.environ.get('PM_AUTH_FILE',
            str(Path.home() / '.config/phonemic/browser-auth.json')))
        self.clock = clock

    @contextlib.contextmanager
    def transaction(self, write=False):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.with_suffix('.lock')
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+') as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if self.path.is_symlink():
                    raise ValueError('Authentication state must not be a symlink')
                if self.path.exists():
                    if self.path.stat().st_mode & 0o077:
                        raise ValueError('Authentication state must have mode 600')
                    state = json.loads(self.path.read_text())
                    if not isinstance(state, dict) or state.get('version') != 1:
                        raise ValueError('Invalid authentication state')
                else:
                    state = {'version': 1}
                yield state
                if write:
                    temp_fd, temp_path = tempfile.mkstemp(dir=self.path.parent, prefix='.browser-auth-')
                    try:
                        with os.fdopen(temp_fd, 'w') as f:
                            json.dump(state, f)
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(temp_path, self.path)
                    finally:
                        if os.path.exists(temp_path):
                            os.unlink(temp_path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def new_code(self):
        code = secrets.token_hex(4).upper()
        with self.transaction(write=True) as state:
            state['pairing'] = {'hash': digest(code), 'expires': self.clock() + PAIR_SECONDS, 'attempts': 0}
        return '-'.join(code[i:i+4] for i in range(0, len(code), 4))

    def pair(self, code, origin):
        token = None
        with self.transaction(write=True) as state:
            pairing = state.get('pairing', {})
            if pairing.get('expires', 0) > self.clock() and pairing.get('attempts', 10) < 10:
                pairing['attempts'] += 1
                normalized = code.replace('-', '').strip().upper()
                if hmac.compare_digest(pairing.get('hash', ''), digest(normalized)):
                    token = secrets.token_urlsafe(32)
                    state['browser'] = {'id': secrets.token_hex(16), 'created': self.clock(),
                        'expires': self.clock() + SESSION_SECONDS, 'cookies': {origin: digest(token)}}
                    state.pop('pairing', None)
                    state.pop('handoff', None)
        return token

    def _valid(self, state, token, origin):
        browser = state.get('browser', {})
        expected = browser.get('cookies', {}).get(origin)
        return bool(isinstance(token, str) and 20 <= len(token) <= 128 and expected
                    and browser.get('expires', 0) > self.clock()
                    and hmac.compare_digest(expected, digest(token)))

    def valid(self, token, origin):
        try:
            with self.transaction() as state:
                return self._valid(state, token, origin)
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def handoff(self, token, origin, target):
        with self.transaction(write=True) as state:
            if not self._valid(state, token, origin):
                return None
            ticket = secrets.token_urlsafe(32)
            state['handoff'] = {'hash': digest(ticket), 'target': target,
                                'expires': self.clock() + 30, 'browser': state['browser']['id']}
            return ticket

    def redeem(self, ticket, origin):
        with self.transaction(write=True) as state:
            handoff, browser = state.get('handoff', {}), state.get('browser', {})
            if (handoff.get('target') == origin and handoff.get('expires', 0) > self.clock()
                    and browser.get('expires', 0) > self.clock()
                    and handoff.get('browser') == browser.get('id')
                    and hmac.compare_digest(handoff.get('hash', ''), digest(ticket))):
                token = secrets.token_urlsafe(32)
                browser['cookies'][origin] = digest(token)
                state.pop('handoff', None)
                return token
        return None

    def revoke(self):
        with self.transaction(write=True) as state:
            state.clear()
            state['version'] = 1

    def status(self):
        with self.transaction() as state:
            browser = state.get('browser', {})
            return {'paired': browser.get('expires', 0) > self.clock(),
                    'expires': browser.get('expires'),
                    'pairing_pending': state.get('pairing', {}).get('expires', 0) > self.clock()}


def cli():
    import argparse
    parser = argparse.ArgumentParser(description='Manage the one approved PhoneMic browser')
    parser.add_argument('action', choices=['pair', 'status', 'revoke'])
    args = parser.parse_args()
    # Match a custom authentication file used by the installed receiver.
    if not os.environ.get('PM_AUTH_FILE'):
        import shlex
        config = Path.home() / '.config/phonemic/web.env'
        if config.exists():
            for line in config.read_text().splitlines():
                if line.startswith('PM_AUTH_FILE='):
                    values = shlex.split(line.split('=', 1)[1])
                    if values:
                        os.environ['PM_AUTH_FILE'] = values[0]
    auth = AuthStore()
    if args.action == 'pair':
        print('Pairing code: ' + auth.new_code())
        print('Enter this on your phone within 5 minutes. A successful pairing replaces the previous browser.')
    elif args.action == 'revoke':
        auth.revoke()
        print('Browser access revoked. Open connections will close within a second.')
    else:
        status = auth.status()
        print('Approved browser: ' + ('yes' if status['paired'] else 'no'))
        if status['paired']:
            print('Expires: ' + time.strftime('%Y-%m-%d', time.localtime(status['expires'])))
        print('Pairing code active: ' + ('yes' if status['pairing_pending'] else 'no'))


if __name__ == '__main__':
    cli()
