import hashlib
import json
import os
import re
import sqlite3
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import Field

from tam_db.contracts import Backend
from team_memory.contracts import DTO, Conflict
from version import VERSION

LOCK_BYTE = b'0'
OPTIONAL_DATABASES = ('learning.db',)
MANIFEST_FILE = 'manifest.json'
POSTGRES_MANIFEST_BACKEND = 'postgres'
RESTORE_ACTOR = 'cli'
DATABASE_PATH = re.compile(r'identity\.db|learning\.db|workspaces/(shared|(?:personal|team)_[a-f0-9]{64})/memory\.db')


class ServerLease:
    def __init__(self, root: Path):
        self.root = root
        self.file = None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = (self.root / '.server.lock').open('a+b')
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.file.seek(0)
            if not self.file.read(1):
                self.file.write(LOCK_BYTE)
                self.file.flush()
        except OSError as exc:
            self.file.close()
            self.file = None
            raise Conflict('Server data is in use; stop the server before offline maintenance') from exc
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.file is not None:
            self.file.close()
            self.file = None


class DatabaseSnapshot(DTO):
    path: str
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class Snapshot(DTO):
    format_version: int = 1
    package_version: str
    created_at: str
    databases: list[DatabaseSnapshot]


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def verify_database(path: Path) -> None:
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise Conflict('Snapshot database integrity check failed')
        if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise Conflict('Snapshot database foreign-key check failed')


def configured_backend(root: Path, environ=None) -> Backend:
    """The server's storage backend without decrypting anything: database.json, else TAM_TEAM_DATABASE_URL, else SQLite."""
    from team_memory.database_config import FileDatabaseConfigStore
    from team_memory.database_contracts import DATABASE_URL_ENV

    env = os.environ if environ is None else environ
    config = FileDatabaseConfigStore(root, env).load()
    if config is not None:
        return config.backend
    return Backend.POSTGRES if env.get(DATABASE_URL_ENV, '').strip() else Backend.SQLITE


def is_postgres_snapshot(snapshot: Path) -> bool:
    try:
        raw = json.loads((snapshot / MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict) and raw.get('backend') == POSTGRES_MANIFEST_BACKEND


def backup(root: Path, destination: Path, environ=None):
    """SQLite: verified copies of every database (server stopped). PostgreSQL: pg_dump of the TAM schemas (online)."""
    root, destination = root.resolve(), destination.resolve()
    if destination == root or root in destination.parents:
        raise Conflict('Backup must be outside server data')
    if configured_backend(root, environ) is Backend.POSTGRES:
        from team_memory import pg_backup
        from team_memory.database_config import FileDatabaseConfigStore

        effective = FileDatabaseConfigStore(root, os.environ if environ is None else environ).effective()
        return pg_backup.backup(effective.dsn, destination, environ)
    return backup_sqlite(root, destination)


def backup_sqlite(root: Path, destination: Path) -> Snapshot:
    root, destination = root.resolve(), destination.resolve()
    if not (root / 'identity.db').is_file():
        raise Conflict('Server identity database does not exist')
    if destination == root or root in destination.parents:
        raise Conflict('Backup must be outside server data')
    with ServerLease(root), ExitStack() as leases:
        optional = [root / name for name in OPTIONAL_DATABASES if (root / name).is_file()]
        sources = [root / 'identity.db', *optional, *sorted((root / 'workspaces').glob('*/memory.db'))]
        for directory in sorted({source.parent for source in sources if source.name == 'memory.db'}):
            leases.enter_context(ServerLease(directory))
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        entries = []
        for source in sources:
            if source.is_symlink() or root not in source.resolve().parents:
                raise Conflict('Database path must remain inside server data')
            relative = source.relative_to(root)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
            target.chmod(0o600)
            verify_database(target)
            entries.append(DatabaseSnapshot(path=relative.as_posix(), sha256=digest(target)))
        manifest = Snapshot(package_version=VERSION, created_at=datetime.now(UTC).isoformat(), databases=entries)
        (destination / MANIFEST_FILE).write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        return manifest


def restore(snapshot: Path, destination: Path, dsn=None, environ=None):
    """A SQLite snapshot becomes the new data directory ``destination``; a PostgreSQL backup is restored
    into the empty database ``dsn`` and ``destination`` is configured to use it."""
    if is_postgres_snapshot(snapshot.resolve()):
        if dsn is None:
            raise Conflict('A PostgreSQL backup is restored into an empty database: pass --dsn-env VAR')
        return restore_postgres(snapshot, destination, dsn, environ)
    if dsn is not None:
        raise Conflict('A SQLite snapshot is restored into a new data directory; --dsn-env is for PostgreSQL backups')
    return restore_sqlite(snapshot, destination)


def database_identity(dsn) -> tuple[tuple[tuple[str, int], ...], str]:
    """Hosts (case-folded, with ports, in any order) and database name: what makes two DSNs name the same database."""
    return tuple(sorted((host.host.lower(), host.port) for host in dsn.hosts)), dsn.database


def refuse_environment_conflict(root: Path, env, dsn) -> None:
    """database.json (written by restore) outranks TAM_TEAM_DATABASE_URL, so restoring into a data
    directory that already runs on PostgreSQL, while the environment names another database, would
    repoint that server. A directory without the PostgreSQL marker is new: the environment is ignored."""
    from team_memory.database_config import read_postgres_marker
    from team_memory.database_contracts import DATABASE_URL_ENV, DatabaseDsn, DsnOrigin, InvalidDsn

    marker = read_postgres_marker(root)
    raw = env.get(DATABASE_URL_ENV, '').strip()
    if marker is None or not raw:
        return
    advice = f'restore into a new data directory (--root), or point {DATABASE_URL_ENV} at the restored database'
    try:
        configured = DatabaseDsn.parse(raw, DsnOrigin.ENV)
    except InvalidDsn as exc:
        raise Conflict(f'{DATABASE_URL_ENV} is not a usable DSN ({exc}) and this data directory runs on '
                       f'{marker.target}; {advice}') from exc
    if database_identity(configured) != database_identity(dsn):
        raise Conflict(f'This data directory runs on {marker.target} through {DATABASE_URL_ENV}, which names '
                       f'another database than the restore target; restoring would repoint the server: {advice}')


def restore_postgres(snapshot: Path, root: Path, dsn, environ=None):
    """pg_restore into ``dsn``, re-provision every workspace role, then point ``root`` at the database.

    ``root`` may exist (it holds master.key) but must hold no databases or database.json: the
    restored installation keeps its id, and the master key must be the one the backup was taken
    with, or the saved provider keys cannot be decrypted.
    """
    from uuid import UUID

    from tam_db.contracts import DatabaseSettings
    from team_memory import pg_backup
    from team_memory.database_config import FileDatabaseConfigStore
    from team_memory.database_contracts import DATABASE_CONFIG_FILE, DatabaseConfig
    from team_memory.pg_provision import PgWorkspaceProvisioner
    from team_memory.settings import load_master_key

    env = os.environ if environ is None else environ
    snapshot, root = snapshot.resolve(), root.resolve()
    if (root / 'identity.db').exists() or (root / DATABASE_CONFIG_FILE).exists():
        raise FileExistsError(root)
    refuse_environment_conflict(root, env, dsn)
    manifest = pg_backup.read_manifest(snapshot)
    if manifest.instance_id is None:
        raise Conflict('The backup names no installation id; it cannot be restored as a TAM server')
    try:
        master_key = load_master_key(root, env, create=False)
    except FileNotFoundError as exc:
        raise Conflict('Restore needs the master key of the backed-up server: set TAM_TEAM_MASTER_KEY '
                       'or copy master.key into the data directory') from exc
    settings = DatabaseSettings.from_environ(env)
    # The lease creates ``root``, so it is taken only once every refusal that needs no lease has passed.
    with ServerLease(root):
        restored = pg_backup.restore(snapshot, dsn, lambda: PgWorkspaceProvisioner(
            dsn.to_uri(), manifest.instance_id, master_key, settings), env)
        store = FileDatabaseConfigStore(root, env)
        store.save(DatabaseConfig(backend=Backend.POSTGRES, dsn_token=store.seal(dsn),
                                  instance_id=UUID(manifest.instance_id), generation=1,
                                  updated_at=datetime.now(UTC), updated_by=RESTORE_ACTOR))
    return restored


def restore_sqlite(snapshot: Path, destination: Path) -> None:
    import shutil

    snapshot, destination = snapshot.resolve(), destination.resolve()
    manifest = Snapshot.model_validate_json((snapshot / MANIFEST_FILE).read_text(encoding="utf-8"))
    if manifest.format_version != 1:
        raise Conflict('Unsupported snapshot format')
    paths = [entry.path for entry in manifest.databases]
    if paths.count('identity.db') != 1 or len(paths) != len(set(paths)):
        raise Conflict('Invalid snapshot database list')
    for entry in manifest.databases:
        if not DATABASE_PATH.fullmatch(entry.path):
            raise Conflict('Invalid snapshot path')
        path = snapshot / entry.path
        if path.is_symlink() or snapshot not in path.resolve().parents or digest(path) != entry.sha256:
            raise Conflict('Snapshot checksum or path validation failed')
        verify_database(path)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with TemporaryDirectory(prefix='.tam-restore-', dir=destination.parent) as temporary:
        staging = Path(temporary) / 'data'
        staging.mkdir(mode=0o700)
        for entry in manifest.databases:
            target = staging / entry.path
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(snapshot / entry.path, target)
            target.chmod(0o600)
            if digest(target) != entry.sha256:
                raise Conflict('Snapshot changed during restore')
            verify_database(target)
        (staging / 'restored-from.json').write_text(json.dumps({'snapshot': manifest.created_at, 'version': manifest.package_version}), encoding="utf-8")
        if destination.exists():
            raise FileExistsError(destination)
        staging.rename(destination)
