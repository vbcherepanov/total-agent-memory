"""Regression tests for the 14.10.0 team-server security review (findings H1, M1-M3, L1-L8, I2, I3)."""
import threading
import time
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from team_memory.accounts import AccountPolicy, Accounts
from team_memory.app import MAX_PENDING_UNAUTHENTICATED, create_app
from team_memory.contracts import (
    Conflict,
    DomainError,
    Forbidden,
    NotFound,
    RateLimited,
    Reply,
    Unauthorized,
    Unavailable,
    Work,
)
from team_memory.dashboard import (
    HSTS_VALUE,
    DashboardConfig,
    client_ip,
    is_https,
)
from team_memory.dashboard_service import DashboardService
from team_memory.metrics import OTHER_METHOD, Metrics, method_label, route_class
from team_memory.registry import (
    SECONDS_PER_DAY,
    TOKEN_USE_RESOLUTION_SECONDS,
    Registry,
    token_ttl_seconds,
)
from team_memory.service import MemoryService
from team_memory.settings import SettingsStore, load_cipher
from team_memory.setup import SetupPolicy, SetupService
from team_memory.worker import DOMAIN_ERRORS, WorkerPool
from tests.team_db_helpers import identity

PASSWORD = 'correct horse battery staple'
WAIT_SECONDS = 10
QUEUE_SECONDS = 0.3


@pytest.fixture(autouse=True)
def backend(team_backend):
    """Every test of this module runs on each selected team backend (--backend)."""
    return team_backend


class Clock:
    def __init__(self):
        self.now = 3_000_000.0

    def __call__(self):
        return self.now


def build(root, config=None, clock=None, token_ttl=None):
    registry = Registry(root, **({'clock': clock} if clock else {}), token_ttl=token_ttl)
    accounts = Accounts(registry, AccountPolicy(), **({'clock': clock} if clock else {}))
    settings = SettingsStore(registry, load_cipher(root, {}), {})
    pool = WorkerPool(registry.root, maximum=2, environment=settings.overrides)
    metrics = Metrics()
    dashboard = DashboardService(registry, accounts, MemoryService(registry, pool), settings, metrics)
    return registry, accounts, metrics, create_app(dashboard.memory, dashboard, config=config)


def make_user(registry, accounts, user_id, role='member'):
    registry.add_user(user_id, user_id.title())
    if role != 'member':
        registry.set_org_role(user_id, role)
    accounts.redeem_invite(user_id, accounts.issue_invite(user_id).code, PASSWORD, '127.0.0.1')


@pytest.fixture
def world(tmp_path):
    registry, accounts, metrics, app = build(tmp_path / 'root')
    make_user(registry, accounts, 'root', 'superadmin')
    make_user(registry, accounts, 'dev')
    with TestClient(app) as client:
        yield SimpleNamespace(registry=registry, accounts=accounts, metrics=metrics, client=client, app=app)


def password_session(client, user_id='root'):
    client.cookies.clear()
    reply = client.post('/dashboard/api/login', json={'user_id': user_id, 'password': PASSWORD})
    assert reply.status_code == 200, reply.text
    return {'X-CSRF-Token': reply.json()['csrf']}


def token_session(client, token):
    client.cookies.clear()
    reply = client.post('/dashboard/api/login/token', json={'token': token})
    assert reply.status_code == 200, reply.text
    return {'X-CSRF-Token': reply.json()['csrf']}


# H1 -----------------------------------------------------------------------------------------------

TOKEN_SESSION_POSTS = (
    ('/dashboard/api/tokens', {'client': 'persist'}),
    ('/dashboard/api/admin/users/root/invite', {}),
    ('/dashboard/api/admin/users', {'id': 'mallory', 'name': 'Mallory'}),
    ('/dashboard/api/admin/users/dev/role', {'org_role': 'superadmin'}),
    ('/dashboard/api/admin/users/dev/active', {'active': False}),
    ('/dashboard/api/admin/teams', {'id': 'evil', 'name': 'Evil'}),
    ('/dashboard/api/admin/teams/eng/rename', {'name': 'Renamed'}),
    ('/dashboard/api/admin/teams/eng/delete', {}),
    ('/dashboard/api/admin/membership', {'user_id': 'dev', 'team_id': 'eng', 'role': 'manager'}),
    ('/dashboard/api/admin/tokens/revoke', {'token_id': 'x'}),
    ('/dashboard/api/admin/settings', {'values': {}}),
    ('/dashboard/api/admin/organization', {'name': 'Evil'}),
    ('/dashboard/api/admin/setup/finish', {}),
    ('/dashboard/api/password', {'current': PASSWORD, 'new': 'another long password!'}),
    ('/dashboard/api/admin/database/test', {'dsn': 'postgresql://u:p@db.example/x'}),
)


@pytest.mark.parametrize('path,body', TOKEN_SESSION_POSTS)
def test_token_session_cannot_mint_credentials_or_change_settings(world, path, body):
    token = world.registry.issue_token('root', 'agent')
    headers = token_session(world.client, token)
    reply = world.client.post(path, headers=headers, json=body)
    assert reply.status_code == 403, reply.text
    assert reply.json()['code'] == 'forbidden' and 'password sign-in' in reply.json()['error']
    assert len(world.registry.tokens_of('root')) == 1
    assert world.accounts.pending_invites() == []
    assert any(c['name'] == 'token_session_denied' for c in world.metrics.snapshot()['counters'])


def test_token_session_keeps_read_access_and_the_same_calls_work_with_a_password(world):
    token = world.registry.issue_token('root', 'agent')
    headers = token_session(world.client, token)
    assert world.client.get('/dashboard/api/tokens').status_code == 200
    assert world.client.get('/dashboard/api/admin/users').status_code == 200
    assert world.client.post('/dashboard/api/tokens/revoke', headers=headers,
                             json={'id': world.registry.tokens_of('root')[0]['id']}).status_code == 200
    headers = password_session(world.client)
    reply = world.client.post('/dashboard/api/tokens', headers=headers, json={'client': 'laptop'})
    assert reply.status_code == 200 and reply.json()['token']
    assert world.client.post('/dashboard/api/admin/users/dev/invite', headers=headers, json={}).status_code == 200


def test_revoking_a_token_ends_its_sessions_and_leaves_other_tokens(tmp_path):
    registry = Registry(tmp_path)
    accounts = Accounts(registry, AccountPolicy())
    registry.add_user('root', 'Root')
    agent = registry.issue_token('root', 'agent')
    unrelated = registry.issue_token('root', 'laptop')
    session = accounts.login_token(agent, 'ip')
    registry.revoke(agent)
    with pytest.raises(Unauthorized):
        registry.authenticate(agent)
    with pytest.raises(Unauthorized):
        accounts.session(session.session_id)
    assert registry.authenticate(unrelated).user_id == 'root'
    assert any(e['action'] == 'token_revoked' for e in registry.audit_events())


def test_revoking_by_id_ends_the_token(tmp_path):
    registry = Registry(tmp_path)
    registry.add_user('root', 'Root')
    agent = registry.issue_token('root', 'agent')
    registry.revoke_token_id(Registry.digest(agent), owner_id='root')
    with pytest.raises(Unauthorized):
        registry.authenticate(agent)


# M1 / L8 ------------------------------------------------------------------------------------------

def test_password_guessing_spread_over_many_ips_locks_the_account(tmp_path):
    clock = Clock()
    registry = Registry(tmp_path)
    accounts = Accounts(registry, AccountPolicy(max_failures=3, max_failures_per_account=8, lock_seconds=300), clock=clock)
    make_user(registry, accounts, 'root', 'superadmin')
    for index in range(8):
        with pytest.raises(Unauthorized):
            accounts.login_password('root', 'wrong password!', f'198.51.100.{index}')
    with pytest.raises(RateLimited):
        accounts.login_password('root', PASSWORD, '198.51.100.200')
    with pytest.raises(Unauthorized):
        # The account-wide lock does not cover invite redemption (the recovery path); this code is just wrong.
        accounts.redeem_invite('root', 'AAAA-AAAA-AAAA-AAAA-AAAA', PASSWORD, '198.51.100.201')
    clock.now += 301
    assert accounts.login_password('root', PASSWORD, '198.51.100.200').actor.user_id == 'root'
    for index in range(7):
        with pytest.raises(Unauthorized):
            accounts.login_password('root', 'wrong password!', f'203.0.113.{index}')
    assert accounts.login_password('root', PASSWORD, '203.0.113.99').actor.user_id == 'root'
    for index in range(7):
        with pytest.raises(Unauthorized):
            accounts.login_password('root', 'wrong password!', f'192.0.2.{index}')
    assert accounts.login_password('root', PASSWORD, '192.0.2.99').actor.user_id == 'root'


def test_account_limit_is_configurable_from_the_environment():
    policy = AccountPolicy.from_env({'TAM_TEAM_LOGIN_MAX_FAILURES_PER_ACCOUNT': '7'})
    assert policy.max_failures_per_account == 7 and AccountPolicy().max_failures_per_account == 20


def request_from(peer, forwarded='', config=None, proto=''):
    headers = [(b'x-forwarded-for', forwarded.encode())] if forwarded else []
    if proto:
        headers.append((b'x-forwarded-proto', proto.encode()))
    app = SimpleNamespace(state=SimpleNamespace(dashboard_config=config or DashboardConfig(trust_proxy=True)))
    return Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': headers, 'query_string': b'',
                    'scheme': 'http', 'server': ('tam', 80), 'client': (peer, 4000) if peer else None, 'app': app})


def test_forwarded_for_is_trusted_only_from_configured_proxies():
    assert client_ip(request_from('203.0.113.9', '1.1.1.1')) == '203.0.113.9'
    assert client_ip(request_from('10.0.0.5', '1.1.1.1')) == '1.1.1.1'
    assert client_ip(request_from('10.0.0.5', '9.9.9.9, 1.1.1.1')) == '1.1.1.1'
    assert client_ip(request_from('10.0.0.5', '9.9.9.9, 1.1.1.1, 10.0.0.7')) == '1.1.1.1'
    assert client_ip(request_from('10.0.0.5', '1.1.1.1, not-an-ip')) == '10.0.0.5'
    assert client_ip(request_from('10.0.0.5', '')) == '10.0.0.5'
    assert client_ip(request_from(None, '1.1.1.1')) == 'unknown'
    assert client_ip(request_from('testclient', '1.1.1.1')) == 'testclient'
    off = DashboardConfig(trust_proxy=False)
    assert client_ip(request_from('10.0.0.5', '1.1.1.1', off)) == '10.0.0.5'
    narrow = DashboardConfig(trust_proxy=True, trusted_proxies=('192.0.2.0/24',))
    assert client_ip(request_from('10.0.0.5', '1.1.1.1', narrow)) == '10.0.0.5'
    assert client_ip(request_from('192.0.2.7', '1.1.1.1', narrow)) == '1.1.1.1'
    assert client_ip(request_from('::1', '2001:db8::7')) == '2001:db8::7'


def test_forwarded_proto_is_trusted_only_from_configured_proxies():
    assert is_https(request_from('10.0.0.5', proto='https'))
    assert not is_https(request_from('203.0.113.9', proto='https'))
    assert not is_https(request_from('10.0.0.5', proto='https', config=DashboardConfig(trust_proxy=False)))


def test_trusted_proxies_come_from_the_environment():
    config = DashboardConfig.from_env({'TAM_TEAM_TRUST_PROXY': 'true', 'TAM_TEAM_TRUSTED_PROXIES': '192.0.2.0/24, 2001:db8::/32'})
    assert config.trusted_proxies == ('192.0.2.0/24', '2001:db8::/32')
    assert DashboardConfig.from_env({}).trusted_proxies == DashboardConfig().trusted_proxies
    with pytest.raises(ValueError):
        DashboardConfig.from_env({'TAM_TEAM_TRUSTED_PROXIES': 'not-a-network'})


def test_rotating_spoofed_forwarded_for_does_not_bypass_the_lockout(tmp_path):
    registry, accounts, _metrics, app = build(tmp_path / 'root', config=DashboardConfig(trust_proxy=True))
    make_user(registry, accounts, 'dev')
    with TestClient(app) as client:
        codes = [client.post('/dashboard/api/login', json={'user_id': 'dev', 'password': 'wrong password!'},
                             headers={'X-Forwarded-For': f'198.51.100.{index}'}).status_code for index in range(7)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]


# M2 -----------------------------------------------------------------------------------------------

class FakeProcess:
    def __init__(self):
        self.stopped = False

    def is_alive(self):
        return not self.stopped

    def terminate(self):
        self.stopped = True

    def kill(self):
        self.stopped = True

    def join(self, _timeout=None):
        return None

    def close(self):
        return None


class FakeConnection:
    """Worker end of the pipe: answers after ``gate`` is set; records what it received."""

    def __init__(self, gate=None, reply=None):
        self.gate, self.reply = gate, reply or Reply(data={'ok': True})
        self.sent, self.closed = [], False
        self.entered = threading.Event()

    def send(self, payload):
        self.sent.append(payload)

    def poll(self, timeout=None):
        self.entered.set()
        return True if self.gate is None else self.gate.wait(timeout)

    def recv(self):
        return self.reply.model_dump_json()

    def close(self):
        self.closed = True


@pytest.fixture
def pool_world(tmp_path):
    registry = Registry(tmp_path)
    registry.add_user('dev', 'Dev')
    registry.add_team('eng', 'Eng')
    registry.membership('dev', 'eng', 'editor')
    token = registry.issue_token('dev', 'agent')
    actor = registry.authenticate(token)
    workspaces = {w.scope.kind.value: w for w in registry.workspaces(actor)}
    pool = WorkerPool(tmp_path, maximum=2, timeout=WAIT_SECONDS, registry=registry, queue_timeout=QUEUE_SECONDS)
    yield SimpleNamespace(registry=registry, token=token, actor=actor, workspaces=workspaces, pool=pool)
    pool.workers.clear()


def work_for(world, workspace, operation='memory_get'):
    return Work(actor=world.actor, workspace=workspace, operation=operation, arguments={'id': 1})


def install(pool, workspace, connection):
    process = FakeProcess()
    pool.workers[workspace.key] = (process, connection)
    return process


def run_async(function, *args):
    result = {}

    def target():
        try:
            result['value'] = function(*args)
        except BaseException as exc:  # noqa: BLE001 - handed to the asserting thread
            result['error'] = exc
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def test_a_slow_call_does_not_block_other_workspaces(pool_world):
    personal, team = pool_world.workspaces['personal'], pool_world.workspaces['team']
    gate = threading.Event()
    slow = FakeConnection(gate=gate)
    install(pool_world.pool, personal, slow)
    install(pool_world.pool, team, FakeConnection())
    thread, result = run_async(pool_world.pool.invoke, work_for(pool_world, personal), pool_world.token)
    assert slow.entered.wait(WAIT_SECONDS)
    started = time.monotonic()
    assert pool_world.pool.invoke(work_for(pool_world, team), pool_world.token) == {'ok': True}
    assert time.monotonic() - started < WAIT_SECONDS / 2 and thread.is_alive()
    gate.set()
    thread.join(WAIT_SECONDS)
    assert result['value'] == {'ok': True}


def test_calls_for_one_workspace_queue_and_time_out_instead_of_waiting_for_the_hung_worker(pool_world):
    personal = pool_world.workspaces['personal']
    gate = threading.Event()
    slow = FakeConnection(gate=gate)
    process = install(pool_world.pool, personal, slow)
    thread, _result = run_async(pool_world.pool.invoke, work_for(pool_world, personal), pool_world.token)
    assert slow.entered.wait(WAIT_SECONDS)
    started = time.monotonic()
    with pytest.raises(Unavailable, match='busy'):
        pool_world.pool.invoke(work_for(pool_world, personal), pool_world.token)
    assert time.monotonic() - started < WAIT_SECONDS / 2
    assert not process.stopped and len(slow.sent) == 1
    gate.set()
    thread.join(WAIT_SECONDS)
    assert pool_world.pool.invoke(work_for(pool_world, personal), pool_world.token) == {'ok': True}


def test_queued_calls_run_one_after_another_without_deadlock(pool_world):
    personal = pool_world.workspaces['personal']
    pool_world.pool.queue_timeout = WAIT_SECONDS
    install(pool_world.pool, personal, FakeConnection())
    threads = [run_async(pool_world.pool.invoke, work_for(pool_world, personal), pool_world.token) for _ in range(8)]
    for thread, _ in threads:
        thread.join(WAIT_SECONDS)
    assert [result.get('value') for _, result in threads] == [{'ok': True}] * 8
    assert pool_world.pool.interest == {}


def test_a_worker_with_a_call_in_flight_is_never_evicted(pool_world):
    personal, team, shared = (pool_world.workspaces[name] for name in ('personal', 'team', 'shared'))
    pool = pool_world.pool
    gates = [threading.Event(), threading.Event()]
    connections = [FakeConnection(gate=gates[0]), FakeConnection(gate=gates[1])]
    processes = [install(pool, personal, connections[0]), install(pool, team, connections[1])]
    threads = [run_async(pool.invoke, work_for(pool_world, personal), pool_world.token),
               run_async(pool.invoke, work_for(pool_world, team), pool_world.token)]
    assert all(connection.entered.wait(WAIT_SECONDS) for connection in connections)
    with pytest.raises(Unavailable, match='busy'):
        pool.call(shared.key, work_for(pool_world, shared), None)
    assert not any(process.stopped for process in processes)
    for gate in gates:
        gate.set()
    for thread, result in threads:
        thread.join(WAIT_SECONDS)
        assert result['value'] == {'ok': True}


def test_many_users_calls_make_progress_in_parallel(tmp_path):
    registry = Registry(tmp_path)
    users = [f'user{index}' for index in range(6)]
    pool = WorkerPool(tmp_path, maximum=len(users), timeout=WAIT_SECONDS, registry=registry, queue_timeout=WAIT_SECONDS)
    barrier = threading.Barrier(len(users), timeout=WAIT_SECONDS)

    class Meeting(FakeConnection):
        def poll(self, timeout=None):
            barrier.wait()
            return True

    try:
        jobs = []
        for user in users:
            registry.add_user(user, user)
            token = registry.issue_token(user, 'agent')
            actor = registry.authenticate(token)
            workspace = next(w for w in registry.workspaces(actor) if w.scope.kind.value == 'personal')
            install(pool, workspace, Meeting())
            jobs.append(run_async(pool.invoke, Work(actor=actor, workspace=workspace, operation='memory_get',
                                                    arguments={'id': 1}), token))
        for thread, result in jobs:
            thread.join(WAIT_SECONDS)
            # Every call reaches the barrier only if all six are in flight at once.
            assert result.get('value') == {'ok': True}, result
    finally:
        pool.workers.clear()


def test_recycle_waits_for_the_call_in_flight_then_stops_the_worker(pool_world):
    personal = pool_world.workspaces['personal']
    pool = pool_world.pool
    gate = threading.Event()
    connection = FakeConnection(gate=gate)
    process = install(pool, personal, connection)
    call, call_result = run_async(pool.invoke, work_for(pool_world, personal), pool_world.token)
    assert connection.entered.wait(WAIT_SECONDS)
    recycled, recycle_result = run_async(pool.recycle)
    recycled.join(QUEUE_SECONDS)
    assert recycled.is_alive() and not process.stopped
    gate.set()
    call.join(WAIT_SECONDS)
    recycled.join(WAIT_SECONDS)
    assert call_result['value'] == {'ok': True} and recycle_result['value'] == 1
    assert process.stopped and pool.workers == {}


@pytest.mark.parametrize('code,error', [('conflict', Conflict), ('forbidden', Forbidden), ('not_found', NotFound),
                                        ('invalid_request', DomainError), ('unavailable', Unavailable),
                                        ('something_new', Unavailable), (None, Unavailable)])
def test_worker_error_codes_keep_their_meaning(pool_world, code, error):
    personal = pool_world.workspaces['personal']
    install(pool_world.pool, personal, FakeConnection(reply=Reply(error='nope', code=code)))
    with pytest.raises(error) as raised:
        pool_world.pool.invoke(work_for(pool_world, personal), pool_world.token)
    assert type(raised.value) is error and set(DOMAIN_ERRORS) >= {'conflict', 'forbidden', 'not_found'}


def test_pool_rejects_a_non_positive_queue_timeout(tmp_path):
    with pytest.raises(ValueError):
        WorkerPool(tmp_path, queue_timeout=0)


def test_workspace_revocation_during_queue_wait_is_enforced(pool_world):
    personal = pool_world.workspaces['personal']
    pool = pool_world.pool
    pool.queue_timeout = WAIT_SECONDS
    gate = threading.Event()
    connection = FakeConnection(gate=gate)
    install(pool, personal, connection)
    first, _ = run_async(pool.invoke, work_for(pool_world, personal), pool_world.token)
    assert connection.entered.wait(WAIT_SECONDS)
    queued, queued_result = run_async(pool.invoke, work_for(pool_world, personal), pool_world.token)
    pool_world.registry.revoke(pool_world.token)
    gate.set()
    first.join(WAIT_SECONDS)
    queued.join(WAIT_SECONDS)
    assert isinstance(queued_result['error'], Unauthorized) and len(connection.sent) == 1


# M3 -----------------------------------------------------------------------------------------------

def test_method_labels_are_a_fixed_set():
    assert [method_label(m) for m in ('GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS')] == \
        ['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS']
    assert method_label('X0') == method_label('get') == method_label('') == OTHER_METHOD


def test_arbitrary_http_methods_do_not_grow_the_metrics(world):
    for index in range(40):
        world.client.request(f'X{index}', '/healthz')
    methods = {dict(c['labels']).get('method') for c in world.metrics.snapshot()['counters']
               if c['name'] == 'http_requests_total'}
    assert methods <= {'GET', 'POST', OTHER_METHOD} and OTHER_METHOD in methods


def test_csrf_rejections_are_labelled_by_route_class_not_raw_path(world):
    password_session(world.client)
    world.client.post('/dashboard/api/admin/users/dev/invite', json={})
    world.client.post('/dashboard/api/admin/users/ghost-42/invite', json={})
    rejected = [c['labels'] for c in world.metrics.snapshot()['counters'] if c['name'] == 'csrf_rejected']
    assert rejected == [{'route': route_class('/dashboard/api/x')}] and rejected[0]['route'] == 'dashboard_api'


# L1 -----------------------------------------------------------------------------------------------

def test_websocket_scopes_are_refused_before_reaching_any_route(world):
    with pytest.raises(WebSocketDisconnect) as refused:
        with world.client.websocket_connect('/mcp/'):
            pass
    assert refused.value.code == 1008
    token = world.registry.issue_token('root', 'agent')
    with pytest.raises(WebSocketDisconnect):
        with world.client.websocket_connect('/mcp/', headers={'Authorization': 'Bearer ' + token}):
            pass


# L2 -----------------------------------------------------------------------------------------------

def test_new_setup_code_lifts_the_shared_setup_lockout(tmp_path):
    clock = Clock()
    registry = Registry(tmp_path)
    metrics = Metrics()
    setup = SetupService(registry, Accounts(registry, AccountPolicy(), clock=clock), metrics,
                         SetupPolicy(max_failures_per_ip=100, max_failures_total=3), clock)
    setup.issue_token()
    for index in range(3):
        with pytest.raises(Unauthorized):
            setup.verify('WRONG-CODE', f'198.51.100.{index}')
    with pytest.raises(RateLimited):
        setup.verify('WRONG-CODE', '198.51.100.99')
    issued = setup.issue_token()
    setup.verify(issued.token, '198.51.100.99')


# L3 -----------------------------------------------------------------------------------------------

def test_expired_session_row_is_kept_while_maintenance_blocks_writes(tmp_path):
    clock = Clock()
    registry = Registry(tmp_path)
    accounts = Accounts(registry, AccountPolicy(), clock=clock)
    registry.add_user('dev', 'Dev')
    session = accounts.redeem_invite('dev', accounts.issue_invite('dev').code, PASSWORD, 'ip')
    maintenance = threading.Event()
    maintenance.set()
    registry.plane.watch_maintenance(maintenance.is_set)

    def sessions():
        with identity(registry) as db:
            return db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]

    clock.now += accounts.policy.session_max_seconds + 1
    with pytest.raises(Unauthorized):
        accounts.session(session.session_id)
    assert sessions() == 1
    maintenance.clear()
    with pytest.raises(Unauthorized):
        accounts.session(session.session_id)
    assert sessions() == 0


def test_revoked_token_session_row_is_kept_while_maintenance_blocks_writes(tmp_path):
    registry = Registry(tmp_path)
    accounts = Accounts(registry, AccountPolicy())
    registry.add_user('dev', 'Dev')
    token = registry.issue_token('dev', 'agent')
    via_token = accounts.login_token(token, 'ip')
    with identity(registry, write=True) as db:
        db.execute('UPDATE tokens SET revoked=1')
    registry.plane.watch_maintenance(lambda: True)
    with pytest.raises(Unauthorized):
        accounts.session(via_token.session_id)
    with identity(registry) as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1


# L4 -----------------------------------------------------------------------------------------------

def test_tokens_expire_and_record_their_last_use(tmp_path):
    clock = Clock()
    registry = Registry(tmp_path, clock=clock, token_ttl=10 * SECONDS_PER_DAY)
    registry.add_user('dev', 'Dev')
    token = registry.issue_token('dev', 'agent')
    listed = registry.tokens_of('dev')[0]
    assert listed['last_used_at'] is None and listed['expires_at'].startswith('1970-02-')
    registry.authenticate(token)
    first = registry.tokens_of('dev')[0]['last_used_at']
    assert first is not None
    clock.now += TOKEN_USE_RESOLUTION_SECONDS - 1
    registry.authenticate(token)
    assert registry.tokens_of('dev')[0]['last_used_at'] == first
    clock.now += 2
    registry.authenticate(token)
    assert registry.tokens_of('dev')[0]['last_used_at'] != first
    clock.now += 10 * SECONDS_PER_DAY
    with pytest.raises(Unauthorized):
        registry.authenticate(token)
    with pytest.raises(Unauthorized):
        Accounts(registry, AccountPolicy(session_max_seconds=100 * SECONDS_PER_DAY), clock=clock).login_token(token, 'ip')


def test_token_session_ends_when_its_token_expires(tmp_path):
    clock = Clock()
    registry = Registry(tmp_path, clock=clock, token_ttl=SECONDS_PER_DAY)
    accounts = Accounts(registry, AccountPolicy(session_idle_seconds=10 * SECONDS_PER_DAY,
                                                session_max_seconds=10 * SECONDS_PER_DAY), clock=clock)
    registry.add_user('dev', 'Dev')
    session = accounts.login_token(registry.issue_token('dev', 'agent'), 'ip')
    clock.now += SECONDS_PER_DAY + 1
    with pytest.raises(Unauthorized):
        accounts.session(session.session_id)


def test_last_use_is_not_written_during_maintenance(tmp_path):
    registry = Registry(tmp_path)
    registry.add_user('dev', 'Dev')
    token = registry.issue_token('dev', 'agent')
    registry.plane.watch_maintenance(lambda: True)
    registry.authenticate(token)
    assert registry.tokens_of('dev')[0]['last_used_at'] is None


def test_token_ttl_environment_and_opt_out(tmp_path):
    assert token_ttl_seconds({}) == 180 * SECONDS_PER_DAY
    assert token_ttl_seconds({'TAM_TEAM_TOKEN_TTL_DAYS': '30'}) == 30 * SECONDS_PER_DAY
    assert token_ttl_seconds({'TAM_TEAM_TOKEN_TTL_DAYS': '0'}) == 0
    with pytest.raises(ValueError):
        token_ttl_seconds({'TAM_TEAM_TOKEN_TTL_DAYS': '-1'})
    registry = Registry(tmp_path, token_ttl=0)
    registry.add_user('dev', 'Dev')
    registry.issue_token('dev', 'agent')
    assert registry.tokens_of('dev')[0]['expires_at'] is None


def test_tokens_created_before_the_upgrade_stay_valid_and_never_expire(tmp_path):
    registry = Registry(tmp_path)
    registry.add_user('dev', 'Dev')
    token = registry.issue_token('dev', 'agent')
    with identity(registry, write=True) as db:
        db.execute('UPDATE tokens SET expires_at=NULL, last_used_at=NULL')
    assert registry.authenticate(token).user_id == 'dev'
    assert registry.tokens_of('dev')[0]['expires_at'] is None


# L5 -----------------------------------------------------------------------------------------------

def test_create_user_is_atomic(world, monkeypatch):
    headers = password_session(world.client)

    def broken(self, db, user_id):
        raise Unavailable('lock timeout')

    monkeypatch.setattr(Accounts, '_write_invite', broken)
    reply = world.client.post('/dashboard/api/admin/users', headers=headers,
                              json={'id': 'newbie', 'name': 'Newbie', 'org_role': 'company_viewer'})
    assert reply.status_code == 503
    assert 'newbie' not in {user['id'] for user in world.registry.list_users()}
    monkeypatch.undo()
    retry = world.client.post('/dashboard/api/admin/users', headers=headers,
                              json={'id': 'newbie', 'name': 'Newbie', 'org_role': 'company_viewer'})
    assert retry.status_code == 200 and retry.json()['code']
    assert world.registry.org_role('newbie') == 'company_viewer'


def test_create_user_rolls_back_when_the_role_step_fails(tmp_path):
    registry = Registry(tmp_path)
    accounts = Accounts(registry, AccountPolicy())
    with pytest.raises(ValueError):
        accounts.create_user('newbie', 'Newbie', 'galactic_emperor')
    assert registry.list_users() == [] and accounts.pending_invites() == []
    with pytest.raises(Conflict):
        accounts.create_user('twin', 'Twin', 'member')
        accounts.create_user('twin', 'Twin', 'member')
    assert len(accounts.pending_invites()) == 1


# L7 -----------------------------------------------------------------------------------------------

def test_bad_credentials_never_cost_a_lookup_beyond_the_pending_cap(world, monkeypatch):
    lookups = []
    original = world.registry.authenticate
    monkeypatch.setattr(world.registry, 'authenticate', lambda token: lookups.append(token) or original(token))
    for bad in ('x', 'short', 'a' * 500, 'bad token with spaces'):
        reply = world.client.post('/api/call', headers={'Authorization': 'Bearer ' + bad}, json={})
        assert reply.status_code == 401
    assert lookups == []
    assert world.client.post('/api/call', headers={'Authorization': 'Bearer ' + 'A' * 43}, json={}).status_code == 401
    assert lookups == ['A' * 43]
    token = world.registry.issue_token('root', 'agent')
    assert world.client.post('/api/call', headers={'Authorization': 'Bearer ' + token},
                             json={'name': 'memory_scopes'}).status_code == 200


def test_unauthenticated_cap_applies_before_lookup(world, monkeypatch):
    lookups = []
    monkeypatch.setattr(world.registry, 'authenticate', lambda token: lookups.append(token))
    authentication = world.app.app
    authentication.unauthenticated = MAX_PENDING_UNAUTHENTICATED
    reply = world.client.post('/api/call', headers={'Authorization': 'Bearer ' + 'A' * 43}, json={})
    assert reply.status_code == 429 and lookups == []
    authentication.unauthenticated = 0
    assert world.client.get('/healthz').status_code == 200
    assert authentication.unauthenticated == 0 and authentication.pending == 0


# I2 / I3 ------------------------------------------------------------------------------------------

def test_health_version_can_be_hidden(tmp_path):
    from server_capabilities import TEAM_CAPABILITIES

    _registry, _accounts, _metrics, app = build(tmp_path / 'a')
    with TestClient(app) as client:
        assert 'version' in client.get('/healthz').json()
        assert client.get('/healthz').json()['capabilities'] == list(TEAM_CAPABILITIES)
    _registry, _accounts, _metrics, app = build(tmp_path / 'b', config=DashboardConfig.from_env(
        {'TAM_TEAM_HEALTH_VERSION': 'false'}))
    with TestClient(app) as client:
        assert client.get('/healthz').json() == {
            'status': 'ok', 'name': 'total-agent-memory', 'capabilities': list(TEAM_CAPABILITIES)}


def test_hsts_is_sent_only_over_https(tmp_path):
    _registry, _accounts, _metrics, app = build(tmp_path / 'a')
    with TestClient(app) as client:
        assert 'strict-transport-security' not in client.get('/healthz').headers
    _registry, _accounts, _metrics, app = build(tmp_path / 'b', config=DashboardConfig(cookie_secure='always'))
    with TestClient(app) as client:
        assert client.get('/healthz').headers['strict-transport-security'] == HSTS_VALUE
        assert client.get('/dashboard/api/session').headers['strict-transport-security'] == HSTS_VALUE
        assert client.post('/api/call', json={}).headers['strict-transport-security'] == HSTS_VALUE
    _registry, _accounts, _metrics, app = build(tmp_path / 'c')
    with TestClient(app, base_url='https://tam.test') as client:
        assert client.get('/healthz').headers['strict-transport-security'] == HSTS_VALUE

