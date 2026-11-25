import hashlib
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from tam_db.contracts import Backend, CompatConnection, ControlKind
from team_memory.contracts import (
    ORG_ROLES,
    OVERSIGHT_ROLES,
    TEAM_ROLES,
    WRITER_ROLES,
    Actor,
    Conflict,
    DomainError,
    Forbidden,
    InvalidInput,
    Scope,
    ScopeKind,
    Unauthorized,
    Workspace,
)
from team_memory.database import SwitchableControlPlane, serializable

REGISTRY_TIMEOUT_SECONDS = 10
TOKEN_BYTES = 32
TOKEN_TTL_ENV = "TAM_TEAM_TOKEN_TTL_DAYS"
DEFAULT_TOKEN_TTL_DAYS = 180
SECONDS_PER_DAY = 24 * 3600
# last_used_at is a staleness hint for admins, so it is written at most this often per token.
TOKEN_USE_RESOLUTION_SECONDS = 3600
MAX_AUDIT_PAGE = 200
CLI_ACTOR = "cli"
AUDIT_ACTOR: ContextVar[str] = ContextVar("team_memory_audit_actor", default=CLI_ACTOR)
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
ORGANIZATION_KEYS = ("name", "public_url", "setup_state")
SETUP_STATES = ("admin_created", "complete")
MAX_URL_CHARS = 512

SCHEMA = f"""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        org_role TEXT NOT NULL DEFAULT 'member' CHECK(org_role IN ('member','company_viewer','superadmin')),
        password_hash TEXT, created_at TEXT, last_login_at TEXT);
    CREATE TABLE IF NOT EXISTS teams (id TEXT PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS membership (
        user_id TEXT REFERENCES users(id), team_id TEXT REFERENCES teams(id),
        role TEXT NOT NULL CHECK(role IN ('reader','editor','manager')),
        PRIMARY KEY(user_id,team_id));
    CREATE TABLE IF NOT EXISTS tokens (
        digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
        client TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT,
        expires_at REAL, last_used_at REAL);
    CREATE TABLE IF NOT EXISTS admin_events (
        id INTEGER PRIMARY KEY, at TEXT NOT NULL DEFAULT ({NOW}),
        action TEXT NOT NULL, subject TEXT NOT NULL, actor TEXT, detail TEXT);
    CREATE TABLE IF NOT EXISTS invites (
        digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL, created_by TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS invites_user ON invites(user_id);
    CREATE TABLE IF NOT EXISTS sessions (
        digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf TEXT NOT NULL, method TEXT NOT NULL CHECK(method IN ('password','token','invite')),
        token_digest TEXT, created_at REAL NOT NULL, last_seen_at REAL NOT NULL, expires_at REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
    CREATE TABLE IF NOT EXISTS login_failures (
        key TEXT PRIMARY KEY, failures INTEGER NOT NULL, window_start REAL NOT NULL,
        locked_until REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS known_devices (
        digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at REAL NOT NULL, last_seen_at REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS known_devices_user ON known_devices(user_id);
    CREATE TABLE IF NOT EXISTS login_log (
        id INTEGER PRIMARY KEY, at REAL NOT NULL, user_id TEXT NOT NULL,
        method TEXT NOT NULL, outcome TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS login_log_at ON login_log(at);
    CREATE TABLE IF NOT EXISTS provider_checks (
        target TEXT NOT NULL, provider TEXT NOT NULL, ok INTEGER NOT NULL, detail TEXT NOT NULL,
        at TEXT NOT NULL DEFAULT ({NOW}), PRIMARY KEY(target, provider));
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, secret INTEGER NOT NULL,
        updated_at TEXT NOT NULL DEFAULT ({NOW}), updated_by TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS organization (
        key TEXT PRIMARY KEY, value TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT ({NOW}), updated_by TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS setup_tokens (
        digest TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
ADDED_COLUMNS = {
    "users": (("org_role", ("TEXT NOT NULL DEFAULT 'member' "
                            "CHECK(org_role IN ('member','company_viewer','superadmin'))")),
              ("password_hash", "TEXT"), ("created_at", "TEXT"), ("last_login_at", "TEXT")),
    "tokens": (("created_at", "TEXT"), ("expires_at", "REAL"), ("last_used_at", "REAL")),
    "admin_events": (("actor", "TEXT"), ("detail", "TEXT")),
}


def token_ttl_seconds(environ: Mapping[str, str] | None = None) -> int:
    """Lifetime of newly issued tokens; 0 (``TAM_TEAM_TOKEN_TTL_DAYS=0``) keeps them valid until revoked."""
    env = os.environ if environ is None else environ
    raw = env.get(TOKEN_TTL_ENV, "").strip()
    days = int(raw) if raw else DEFAULT_TOKEN_TTL_DAYS
    if days < 0:
        raise ValueError(TOKEN_TTL_ENV + " must be zero or positive")
    return days * SECONDS_PER_DAY


def _iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC).isoformat()


def _like_prefix(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Registry:
    """Identity control plane. Without ``plane`` it opens the configured one (database.json >
    TAM_TEAM_DATABASE_URL > SQLite); on PostgreSQL the schema is provisioned by pg_provision."""

    def __init__(self, root: Path, plane: SwitchableControlPlane | None = None,
                 clock: Callable[[], float] = time.time, token_ttl: int | None = None):
        self.clock = clock
        self.token_ttl = token_ttl_seconds() if token_ttl is None else token_ttl
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if plane is None:
            from team_memory.database_config import open_control_plane
            plane = open_control_plane(self.root)
        self.plane = plane
        self.path = self.root / "identity.db"
        if plane.backend is Backend.SQLITE:
            self._migrate()
            self.path.chmod(0o600)

    def _migrate(self) -> None:
        db = sqlite3.connect(self.path, timeout=REGISTRY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table, columns in ADDED_COLUMNS.items():
                    if table not in existing:
                        continue
                    present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                    for name, definition in columns:
                        if name not in present:
                            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                membership = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='membership'").fetchone()
                if membership is not None and "'manager'" not in membership[0]:
                    db.execute("""CREATE TABLE membership_v2 (
                        user_id TEXT REFERENCES users(id), team_id TEXT REFERENCES teams(id),
                        role TEXT NOT NULL CHECK(role IN ('reader','editor','manager')),
                        PRIMARY KEY(user_id,team_id))""")
                    db.execute("INSERT INTO membership_v2(user_id,team_id,role) SELECT user_id,team_id,role FROM membership")
                    copied = db.execute("SELECT COUNT(*) FROM membership_v2").fetchone()[0]
                    if copied != db.execute("SELECT COUNT(*) FROM membership").fetchone()[0]:
                        raise Conflict("Membership migration lost rows")
                    db.execute("DROP TABLE membership")
                    db.execute("ALTER TABLE membership_v2 RENAME TO membership")
                    db.execute("INSERT INTO admin_events(action,subject,actor) VALUES ('schema_migrated','membership_roles','system')")
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        finally:
            db.close()

    def connect(self, *, write: bool = False):
        """One identity transaction; ``write`` takes SQLite's write lock up front (BEGIN IMMEDIATE)
        for read-then-write checks. PostgreSQL transactions are SERIALIZABLE either way."""
        return self.plane.connect(ControlKind.IDENTITY, write=write)

    @staticmethod
    @contextmanager
    def acting_as(actor_id: str):
        context = AUDIT_ACTOR.set(actor_id)
        try:
            yield
        finally:
            AUDIT_ACTOR.reset(context)

    @serializable
    def add_user(self, user_id: str, name: str) -> None:
        with self.connect() as db:
            self.insert_user(db, user_id, name)

    @classmethod
    def insert_user(cls, db: CompatConnection, user_id: str, name: str) -> None:
        cls._identifier(user_id)
        cls._name(name)
        try:
            db.execute(f"INSERT INTO users(id,name,created_at) VALUES (?,?,{NOW})", (user_id, name))
        except sqlite3.IntegrityError as exc:
            raise Conflict("User already exists") from exc
        cls._event(db, "user_created", user_id)

    @serializable
    def set_active(self, user_id: str, active: bool) -> dict[str, int]:
        """The single offboarding path (dashboard and CLI).

        Disabling revokes every token of the user, ends every session and voids unused invites, so
        nothing issued before stays valid if the user is enabled again. Enabling only lifts the block:
        the user needs a new invite or token.
        """
        with self.connect(write=True) as db:
            self._require_user(db, user_id)
            tokens = sessions = invites = 0
            if not active:
                self._keep_superadmin(db, user_id)
                sessions = db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,)).rowcount
                db.execute("DELETE FROM known_devices WHERE user_id=?", (user_id,))
                tokens = db.execute("UPDATE tokens SET revoked=1 WHERE user_id=? AND revoked=0", (user_id,)).rowcount
                invites = db.execute("DELETE FROM invites WHERE user_id=? AND used_at IS NULL", (user_id,)).rowcount
            db.execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))
            self._event(db, "user_enabled" if active else "user_disabled", user_id,
                        "" if active else f"tokens_revoked={tokens} sessions_ended={sessions} invites_voided={invites}")
        return {"tokens_revoked": tokens, "sessions_ended": sessions, "invites_voided": invites}

    @serializable
    def org_role(self, user_id: str) -> str:
        with self.connect() as db:
            row = db.execute("SELECT org_role FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise DomainError("Unknown user")
        return row["org_role"]

    @serializable
    def set_org_role(self, user_id: str, role: str) -> None:
        with self.connect(write=True) as db:
            self.apply_org_role(db, user_id, role)

    @classmethod
    def apply_org_role(cls, db: CompatConnection, user_id: str, role: str) -> None:
        if role not in ORG_ROLES:
            raise InvalidInput("org_role must be member, company_viewer or superadmin")
        cls._require_user(db, user_id)
        if role != "superadmin":
            cls._keep_superadmin(db, user_id)
        db.execute("UPDATE users SET org_role=? WHERE id=?", (role, user_id))
        cls._event(db, "org_role:" + role, user_id)

    @serializable
    def add_team(self, team_id: str, name: str) -> None:
        self._identifier(team_id)
        self._name(name)
        with self.connect() as db:
            try:
                db.execute("INSERT INTO teams(id,name) VALUES (?,?)", (team_id, name))
            except sqlite3.IntegrityError as exc:
                raise Conflict("Team already exists") from exc
            self._event(db, "team_created", team_id)

    @serializable
    def rename_team(self, team_id: str, name: str) -> None:
        self._name(name)
        with self.connect() as db:
            if db.execute("UPDATE teams SET name=? WHERE id=?", (name, team_id)).rowcount == 0:
                raise DomainError("Unknown team")
            self._event(db, "team_renamed", team_id)

    @serializable
    def delete_team(self, team_id: str) -> None:
        if self.plane.workspaces.exists(self.team_workspace_key(team_id)):
            raise Conflict("Team has stored memory and cannot be deleted")
        with self.connect(write=True) as db:
            if db.execute("SELECT 1 FROM membership WHERE team_id=?", (team_id,)).fetchone():
                raise Conflict("Remove all members before deleting the team")
            if db.execute("DELETE FROM teams WHERE id=?", (team_id,)).rowcount == 0:
                raise DomainError("Unknown team")
            self._event(db, "team_deleted", team_id)

    @serializable
    def membership(self, user_id: str, team_id: str, role: str | None) -> None:
        if role is not None and role not in TEAM_ROLES:
            raise InvalidInput("role must be reader, editor or manager")
        with self.connect() as db:
            if role is None:
                db.execute("DELETE FROM membership WHERE user_id=? AND team_id=?", (user_id, team_id))
            else:
                try:
                    db.execute("INSERT INTO membership VALUES (?,?,?) ON CONFLICT(user_id,team_id) "
                               "DO UPDATE SET role=excluded.role", (user_id, team_id, role))
                except sqlite3.IntegrityError as exc:
                    raise DomainError("Unknown user or team") from exc
            self._event(db, "membership:" + str(role), user_id + ":" + team_id)

    @serializable
    def team_role(self, user_id: str, team_id: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT role FROM membership WHERE user_id=? AND team_id=?", (user_id, team_id)).fetchone()
        return None if row is None else row["role"]

    @serializable
    def teams_of(self, user_id: str) -> list[tuple[str, str]]:
        with self.connect() as db:
            rows = db.execute("SELECT team_id,role FROM membership WHERE user_id=? ORDER BY team_id", (user_id,)).fetchall()
        return [(row["team_id"], row["role"]) for row in rows]

    @serializable
    def team_members(self, team_id: str) -> list[tuple[str, str, str]]:
        with self.connect() as db:
            rows = db.execute("SELECT u.id,u.name,m.role FROM membership m JOIN users u ON u.id=m.user_id "
                              "WHERE m.team_id=? ORDER BY u.id", (team_id,)).fetchall()
        return [(row["id"], row["name"], row["role"]) for row in rows]

    @serializable
    def team_exists(self, team_id: str) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM teams WHERE id=?", (team_id,)).fetchone() is not None

    @serializable
    def can_view_team_people(self, actor: Actor, team_id: str) -> bool:
        if not self.team_exists(team_id):
            return False
        with self.connect() as db:
            row = db.execute("SELECT org_role FROM users WHERE id=? AND active=1", (actor.user_id,)).fetchone()
        if row is None:
            return False
        return row["org_role"] in OVERSIGHT_ROLES or self.team_role(actor.user_id, team_id) == "manager"

    @serializable
    def list_teams(self) -> list[tuple[str, str]]:
        with self.connect() as db:
            return [(row["id"], row["name"]) for row in db.execute("SELECT id,name FROM teams ORDER BY id")]

    def teams(self) -> list[tuple[str, str]]:
        return self.list_teams()

    @serializable
    def list_users(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id,name,active,org_role,password_hash IS NOT NULL AS password_set,"
                              "created_at,last_login_at FROM users ORDER BY id").fetchall()
        return [{"id": row["id"], "name": row["name"], "active": bool(row["active"]), "org_role": row["org_role"],
                 "password_set": bool(row["password_set"]), "created_at": row["created_at"],
                 "last_login_at": row["last_login_at"]} for row in rows]

    @serializable
    def issue_token(self, user_id: str, client: str) -> str:
        self._name(client)
        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = self.clock()
        expires = now + self.token_ttl if self.token_ttl else None
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM users WHERE id=? AND active=1", (user_id,)).fetchone():
                raise Forbidden("Unknown or disabled user")
            db.execute(f"INSERT INTO tokens(digest,user_id,client,created_at,expires_at) "
                       f"VALUES (?,?,?,{NOW},?)", (self.digest(token), user_id, client, expires))
            self._event(db, "token_created", user_id + ":" + client)
        return token

    @serializable
    def revoke(self, token: str) -> None:
        with self.connect() as db:
            self._revoke_one(db, self.digest(token))

    @serializable
    def revoke_token_id(self, token_id: str, owner_id: str | None = None) -> None:
        with self.connect() as db:
            row = db.execute("SELECT user_id FROM tokens WHERE digest=?", (token_id,)).fetchone()
            if row is None or (owner_id is not None and row["user_id"] != owner_id):
                raise DomainError("Unknown token")
            self._revoke_one(db, token_id)

    @classmethod
    def _revoke_one(cls, db: CompatConnection, digest: str) -> None:
        db.execute("UPDATE tokens SET revoked=1 WHERE digest=?", (digest,))
        db.execute("DELETE FROM sessions WHERE token_digest=?", (digest,))
        cls._event(db, "token_revoked", digest)

    @serializable
    def tokens_of(self, user_id: str | None = None) -> list[dict]:
        query = "SELECT digest,user_id,client,revoked,created_at,expires_at,last_used_at FROM tokens"
        with self.connect() as db:
            rows = (db.execute(query + " WHERE user_id=? ORDER BY created_at DESC", (user_id,)) if user_id
                    else db.execute(query + " ORDER BY user_id, created_at DESC")).fetchall()
        return [{"id": row["digest"], "user_id": row["user_id"], "client": row["client"],
                 "revoked": bool(row["revoked"]), "created_at": row["created_at"],
                 "expires_at": _iso(row["expires_at"]), "last_used_at": _iso(row["last_used_at"])} for row in rows]

    @serializable
    def authenticate(self, token: str) -> Actor:
        digest, now = self.digest(token), self.clock()
        with self.connect() as db:
            row = db.execute("SELECT u.id,u.name,u.org_role,t.client,t.last_used_at FROM tokens t "
                             "JOIN users u ON u.id=t.user_id WHERE digest=? AND revoked=0 AND active=1 "
                             "AND (t.expires_at IS NULL OR t.expires_at>?)", (digest, now)).fetchone()
            if row is not None and (row["last_used_at"] is None or row["last_used_at"] + TOKEN_USE_RESOLUTION_SECONDS <= now) \
                    and not self.plane.maintenance_active():
                db.execute("UPDATE tokens SET last_used_at=? WHERE digest=?", (now, digest))
        if row is None:
            raise Unauthorized("Invalid or revoked token")
        return Actor(user_id=row["id"], display_name=row["name"], client=row["client"], org_role=row["org_role"])

    def workspaces(self, actor: Actor) -> list[Workspace]:
        result = [Workspace(key="personal_" + self.digest(actor.user_id), scope=Scope(), owner_id=actor.user_id, writable=True)]
        result.extend(Workspace(key=self.team_workspace_key(team_id),
                                scope=Scope(kind=ScopeKind.team, team_id=team_id),
                                writable=role in WRITER_ROLES) for team_id, role in self.teams_of(actor.user_id))
        result.append(Workspace(key="shared", scope=Scope(kind=ScopeKind.shared), writable=True))
        return result

    def authorize(self, actor: Actor, scope: Scope, write: bool) -> Workspace:
        for workspace in self.workspaces(actor):
            if workspace.scope == scope:
                if write and not workspace.writable:
                    raise Forbidden("Workspace is read-only")
                return workspace
        if scope.kind == ScopeKind.team and self.team_exists(scope.team_id) and \
                self.org_role(actor.user_id) in OVERSIGHT_ROLES:
            if write:
                raise Forbidden("Workspace is read-only")
            return Workspace(key=self.team_workspace_key(scope.team_id), scope=scope, writable=False)
        raise Forbidden("Workspace unavailable")

    @serializable
    def audit_events(self, before: int | None = None, limit: int = 50, actor: str | None = None,
                     action: str | None = None, subject: str | None = None) -> list[dict]:
        limit = max(1, min(limit, MAX_AUDIT_PAGE))
        query, args = "SELECT id,at,action,subject,actor,detail FROM admin_events WHERE id<?", [before or 2**62]
        if actor:
            query, args = query + " AND actor=?", [*args, actor]
        if action:
            query, args = query + " AND action LIKE ? ESCAPE '\\'", [*args, _like_prefix(action)]
        if subject:
            query, args = query + " AND subject LIKE ? ESCAPE '\\'", [*args, "%" + _like_prefix(subject)]
        with self.connect() as db:
            rows = db.execute(query + " ORDER BY id DESC LIMIT ?", [*args, limit]).fetchall()
        return [dict(row) for row in rows]

    @serializable
    def audit_actions(self) -> list[str]:
        with self.connect() as db:
            return [row[0] for row in db.execute("SELECT DISTINCT action FROM admin_events ORDER BY action")]

    @serializable
    def has_superadmin(self) -> bool:
        with self.connect() as db:
            return self.superadmin_exists(db)

    @serializable
    def organization(self) -> dict[str, str]:
        with self.connect() as db:
            rows = db.execute("SELECT key,value FROM organization").fetchall()
        return {row["key"]: row["value"] for row in rows if row["key"] in ORGANIZATION_KEYS}

    @serializable
    def set_organization(self, values: Mapping[str, str]) -> None:
        with self.connect() as db:
            self.write_organization(db, values)

    @classmethod
    def write_organization(cls, db: CompatConnection, values: Mapping[str, str]) -> None:
        unknown = sorted(set(values) - set(ORGANIZATION_KEYS))
        if unknown:
            raise DomainError("Unknown organization field: " + ", ".join(unknown))
        prepared = {key: cls._organization_value(key, value) for key, value in values.items()}
        for key, value in prepared.items():
            db.execute("INSERT INTO organization(key,value,updated_by) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                       f"value=excluded.value,updated_by=excluded.updated_by,updated_at={NOW}",
                       (key, value, AUDIT_ACTOR.get()))
            cls._event(db, "organization_updated", key, value)

    @staticmethod
    def superadmin_exists(db: CompatConnection) -> bool:
        return db.execute("SELECT 1 FROM users WHERE org_role='superadmin' AND active=1").fetchone() is not None

    @classmethod
    def _organization_value(cls, key: str, value: str) -> str:
        value = value.strip()
        if key == "name":
            cls._name(value)
        elif key == "public_url":
            parts = urlsplit(value)
            if len(value) > MAX_URL_CHARS or parts.scheme not in ("http", "https") or not parts.hostname \
                    or parts.username or parts.password or parts.query or parts.fragment:
                raise DomainError("Public URL must be an http(s) URL without credentials, query or fragment")
            value = value.rstrip("/")
        elif value not in SETUP_STATES:
            raise DomainError("setup_state must be one of " + ", ".join(SETUP_STATES))
        return value

    @serializable
    def record_event(self, action: str, subject: str, detail: str = "") -> None:
        with self.connect() as db:
            self._event(db, action, subject, detail)

    def team_workspace_key(self, team_id: str) -> str:
        return "team_" + self.digest(team_id)

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _identifier(value: str) -> None:
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value) is None:
            raise InvalidInput("ID must contain 1–64 letters, digits, underscores or hyphens")

    @staticmethod
    def _name(value: str) -> None:
        if not value.strip() or len(value) > 128:
            raise InvalidInput("Name must contain 1–128 characters")

    @staticmethod
    def _require_user(db: CompatConnection, user_id: str) -> None:
        if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
            raise DomainError("Unknown user")

    @staticmethod
    def _keep_superadmin(db: CompatConnection, user_id: str) -> None:
        others = db.execute("SELECT COUNT(*) FROM users WHERE org_role='superadmin' AND active=1 AND id<>?",
                            (user_id,)).fetchone()[0]
        current = db.execute("SELECT org_role='superadmin' AND active=1 FROM users WHERE id=?", (user_id,)).fetchone()[0]
        if current and others == 0:
            raise Conflict("At least one active superadmin must remain")

    @staticmethod
    def _event(db: CompatConnection, action: str, subject: str, detail: str = "") -> None:
        db.execute("INSERT INTO admin_events(action,subject,actor,detail) VALUES (?,?,?,?)",
                   (action, subject, AUDIT_ACTOR.get(), detail))
