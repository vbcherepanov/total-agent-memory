import argparse
import json
import os
import platform
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from typing import Callable

TIMEOUT = 180
START_TIMEOUT = 45
STOP_TIMEOUT = 45
WATCH_TIMEOUT = 15
WATCH_POLL_SECONDS = 0.05


class Scenario:
    def __init__(self, root, layout, url=None):
        self.root = root
        self.url = url
        self.layout = layout
        self.env = {**os.environ, 'MEMORY_LLM_ENABLED': 'false', 'MEMORY_MODE': 'fast',
                    'MEMORY_QUALITY_GATE_ENABLED': 'false', 'MEMORY_ASYNC_ENRICHMENT': 'false',
                    'MEMORY_EMBED_THREADS': '1', 'MCP_TRANSPORT': 'stdio',
                    'PYTHONIOENCODING': 'utf-8'}
        if layout == 'image':
            self.team = [sys.executable, '/app/src/team_memory/cli.py']
            self.remote = [sys.executable, '/app/src/team_memory/remote.py']
            self.local = [sys.executable, '/app/src/server.py']
            self.env['PYTHONPATH'] = '/app/src'
        else:
            self.env.pop('PYTHONPATH', None)
            self.team = [self.executable('tam-team')]
            self.remote = [self.executable('tam-remote')]
            self.local = [self.executable('tam')]
        self.server_command = (self.team if layout == 'image' else
                               [sys.executable, '-c', 'from total_agent_memory.team import main; main()'])

    @staticmethod
    def executable(name):
        path = shutil.which(name)
        if path is None:
            raise RuntimeError(f'Missing installed entry point: {name}')
        return path

    def cli(self, *arguments, root=None):
        return subprocess.run([*self.team, '--root', str(root or self.root), *map(str, arguments)],
                              env=self.env, cwd=tempfile.gettempdir(), check=True,
                              capture_output=True, text=True, encoding='utf-8', timeout=TIMEOUT)

    @contextmanager
    def server(self):
        if self.url is not None:
            yield
            return
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen([*self.server_command, '--root', str(self.root), 'serve', '--port', str(port)],
                                       env=self.env, cwd=tempfile.gettempdir(), stdout=log, stderr=log,
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
            try:
                deadline = time.monotonic() + START_TIMEOUT
                while True:
                    try:
                        with urllib.request.urlopen(self.url + '/healthz', timeout=2) as response:
                            assert response.status == 200
                        break
                    except (OSError, urllib.error.URLError):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            raise RuntimeError(log.read().decode('utf-8', errors='replace')) from None
                        time.sleep(0.1)
                yield
            except Exception:
                log.seek(0)
                sys.stderr.write(log.read().decode('utf-8', errors='replace'))
                raise
            finally:
                if os.name == 'nt':
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
                self.url = None
                try:
                    process.wait(timeout=STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=STOP_TIMEOUT)
                    log.seek(0)
                    raise RuntimeError(f'Server did not stop within {STOP_TIMEOUT} s of the stop signal; log:\n'
                                       + log.read().decode('utf-8', errors='replace'))

    def call(self, user, name, arguments):
        token = (self.root / f'{user}.token').read_text(encoding="utf-8").strip()
        request = urllib.request.Request(self.url + '/api/call', method='POST',
                    data=json.dumps({'name': name, 'arguments': arguments}).encode(),
                    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            sys.stderr.write(exc.read().decode('utf-8', errors='replace') + '\n')
            raise

    def stdio(self, command, env, name, arguments, before_call: Callable[[], None] | None = None):
        frames = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-06-18', 'capabilities': {},
                'clientInfo': {'name': 'release-smoke', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
             'params': {'name': name, 'arguments': arguments}},
        ]
        replies = {}
        with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as errors:
            process = subprocess.Popen(command, env=env, cwd=tempfile.gettempdir(),
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                        text=True, encoding='utf-8')
            reader = ThreadPoolExecutor(max_workers=1)
            try:
                for frame in frames:
                    if frame['method'] == 'tools/call' and before_call is not None:
                        before_call()
                    process.stdin.write(json.dumps(frame) + '\n')
                    process.stdin.flush()
                    if 'id' not in frame:
                        continue
                    deadline = time.monotonic() + TIMEOUT
                    while frame['id'] not in replies:
                        line = reader.submit(process.stdout.readline).result(
                            timeout=max(0, deadline - time.monotonic()))
                        if not line:
                            errors.seek(0)
                            raise AssertionError('MCP closed before reply: ' + errors.read())
                        item = json.loads(line)
                        if 'id' in item:
                            replies[item['id']] = item
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=START_TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=START_TIMEOUT)
                    raise
                finally:
                    process.stdout.close()
                    reader.shutdown(wait=True)
            if process.returncode != 0:
                errors.seek(0)
                raise AssertionError(f'MCP exited with {process.returncode}; stderr:\n{errors.read()}')
        assert 'protocolVersion' in replies[1]['result'], replies[1]
        assert 'error' not in replies[2], replies[2]
        assert not replies[2]['result'].get('isError'), replies[2]
        return replies[2]['result']

    def seed(self):
        for user, name in [('vasya', 'Вася'), ('petya', 'Петя')]:
            self.cli('user-add', user, name)
            self.cli('token-create', user, '--client', 'release-smoke', '--out', self.root / f'{user}.token')
        self.cli('team-add', 'engineering', 'Разработка')
        for user in ('vasya', 'petya'):
            self.cli('member', user, 'engineering', 'editor')

    def exercise(self):
        records = []
        for scope in ({'kind': 'personal'}, {'kind': 'team', 'team_id': 'engineering'}, {'kind': 'shared'}):
            args = {'scope': scope, 'content': 'Релиз Orion хранит данные в SQLite WAL.', 'request_id': str(uuid4())}
            saved = self.call('vasya', 'memory_save', args)['data']
            assert saved['saved']
            assert saved['created_by']['user_id'] == 'vasya'
            assert self.call('vasya', 'memory_save', args)['data'] == saved
            records.append({'scope': scope, 'id': saved['id'], 'revision': saved['revision']})
        team = records[1]
        update_args = {'scope': team['scope'], 'id': team['id'], 'expected_revision': team['revision'],
                       'content': 'Релиз Orion использует SQLite WAL и резервные копии.',
                       'reason': 'Уточнение плана', 'request_id': str(uuid4())}
        updated = self.call('petya', 'memory_update', update_args)['data']
        assert updated['created_by']['user_id'] == 'vasya'
        assert updated['updated_by']['user_id'] == 'petya'
        records[1] = {'scope': team['scope'], 'id': updated['id'], 'revision': updated['revision']}
        self.verify(records)
        env = {**self.env, 'TAM_REMOTE_URL': self.url + '/mcp',
               'PYTHONIOENCODING': 'ascii',
               'TAM_REMOTE_TOKEN_FILE': str(self.root / 'petya.token')}
        remote = self.stdio(self.remote, env, 'memory_get', {'scope': team['scope'], 'id': updated['id']})
        assert json.loads(remote['content'][0]['text'])['data']['updated_by']['user_id'] == 'petya'
        return records

    def verify(self, records):
        for record in records:
            args = {'scope': record['scope'], 'id': record['id']}
            actual = self.call('vasya', 'memory_get', args)['data']
            assert actual['revision'] == record['revision']
            assert actual['created_by']['user_id'] == 'vasya'
        history = self.call('petya', 'memory_history', {'scope': records[1]['scope'], 'id': records[1]['id']})['data']
        assert any(item['actor']['user_id'] == 'petya' for item in history)
        result = self.call('petya', 'memory_recall', {'query': 'Orion SQLite', 'limit': 20})
        assert result['results']
        assert {item['scope']['kind'] for item in result['results']} == {'team', 'shared'}
        self.cli('member', 'petya', 'engineering', 'remove')
        try:
            self.call('petya', 'memory_get', {'scope': records[1]['scope'], 'id': records[1]['id']})
        except urllib.error.HTTPError as exc:
            assert exc.code in (400, 403)
        else:
            raise AssertionError('Removed member retained access')
        finally:
            self.cli('member', 'petya', 'engineering', 'editor')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--layout', choices=('wheel', 'image'), default='wheel')
    parser.add_argument('--server-url')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--verify-existing', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='tam release Юникод ') as directory:
        root = args.root or Path(directory) / 'server'
        scenario = Scenario(root, args.layout, args.server_url)
        state = root / 'smoke-state.json'
        if args.verify_existing:
            with scenario.server():
                scenario.verify(json.loads(state.read_text(encoding="utf-8")))
        else:
            scenario.seed()
            with scenario.server():
                records = scenario.exercise()
            state.write_text(json.dumps(records), encoding="utf-8")
            if args.server_url is None:
                with scenario.server():
                    scenario.verify(records)
                snapshot = Path(directory) / 'snapshot'
                recovered = Path(directory) / 'recovered'
                scenario.cli('backup', '--out', snapshot)
                scenario.cli('restore', '--from', snapshot, root=recovered)
                for user in ('vasya', 'petya'):
                    shutil.copyfile(root / f'{user}.token', recovered / f'{user}.token')
                scenario.root = recovered
                with scenario.server():
                    scenario.verify(records)
                local_env = {**scenario.env, 'TAM_MEMORY_DIR': str(Path(directory) / 'local'),
                             'TAM_INDEX_WATCH_INTERVAL_SECONDS': '0.1'}
                scenario.stdio(scenario.local, local_env, 'memory_save',
                               {'content': 'Native package smoke retains Unicode: Вася, Петя.', 'type': 'fact'})
                resources = Path(local_env['TAM_MEMORY_DIR']) / 'resources'
                resources.mkdir()
                (resources / 'notes.unlisted_format').write_text('InstalledResourceSentinel', encoding='utf-8')
                indexed = scenario.stdio(scenario.local, local_env, 'ingest_codebase',
                                         {'path': str(resources), 'project': 'package-smoke'})
                assert json.loads(indexed['content'][0]['text'])['persisted']
                # Each stdio call starts a new process: this also verifies index persistence.
                searched = scenario.stdio(scenario.local, local_env, 'ingest_codebase',
                                          {'path': str(resources), 'project': 'package-smoke',
                                           'action': 'search', 'query': 'InstalledResourceSentinel'})
                assert json.loads(searched['content'][0]['text'])['count'] > 0
                def change_and_wait():
                    (resources / 'notes.unlisted_format').write_text('AutomaticResourceSentinel', encoding='utf-8')
                    database = sqlite3.connect(Path(local_env['TAM_MEMORY_DIR']) / 'memory.db')
                    try:
                        deadline = time.monotonic() + WATCH_TIMEOUT
                        while time.monotonic() < deadline:
                            if database.execute('SELECT COUNT(*) FROM resource_chunks WHERE content=?',
                                                ('AutomaticResourceSentinel',)).fetchone()[0]:
                                return
                            time.sleep(WATCH_POLL_SECONDS)
                        raise AssertionError('Installed background watcher did not update the index')
                    finally:
                        database.close()
                updated = scenario.stdio(scenario.local, local_env, 'ingest_codebase',
                                         {'path': str(resources), 'project': 'package-smoke',
                                          'action': 'search', 'query': 'AutomaticResourceSentinel'},
                                         before_call=change_and_wait)
                assert json.loads(updated['content'][0]['text'])['count'] > 0
        sys.stdout.write(json.dumps({'status': 'passed', 'os': platform.system(),
                                    'machine': platform.machine(), 'python': platform.python_version(),
                                    'layout': args.layout, 'external_server': bool(args.server_url)}) + '\n')


if __name__ == '__main__':
    main()
