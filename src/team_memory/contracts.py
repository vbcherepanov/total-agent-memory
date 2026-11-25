from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

MAX_CONTENT_CHARS = 200_000
MAX_QUERY_CHARS = 8_000
MAX_RESULTS = 50


class DomainError(Exception):
    code = "invalid_request"


class InvalidInput(DomainError, ValueError):
    """Malformed user-supplied value; stays a ValueError for callers that already catch that."""


class Unauthorized(DomainError):
    code = "unauthorized"


class Forbidden(DomainError):
    code = "forbidden"


class ReadOnly(DomainError):
    code = "readonly"


class Conflict(DomainError):
    code = "conflict"


class Unavailable(DomainError):
    code = "unavailable"


class RateLimited(DomainError):
    code = "rate_limited"


class NotFound(DomainError):
    code = "not_found"


ORG_ROLES = ("member", "company_viewer", "superadmin")
TEAM_ROLES = ("reader", "editor", "manager")
WRITER_ROLES = frozenset(("editor", "manager"))
OVERSIGHT_ROLES = frozenset(("company_viewer", "superadmin"))


class ScopeKind(str, Enum):
    personal = "personal"
    team = "team"
    shared = "shared"


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Actor(DTO):
    user_id: str
    display_name: str
    client: str
    org_role: Literal["member", "company_viewer", "superadmin"] = "member"


class Scope(DTO):
    kind: ScopeKind = ScopeKind.personal
    team_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def validate_team(self):
        if (self.kind == ScopeKind.team) != (self.team_id is not None):
            raise ValueError("team_id is required only for team scope")
        return self

    def system_tags(self) -> list[str]:
        tags = ["scope:" + self.kind.value]
        if self.team_id is not None:
            tags.append("team:" + self.team_id)
        return tags


class Workspace(DTO):
    key: str
    scope: Scope
    owner_id: str | None = None
    writable: bool


class Save(DTO):
    request_id: UUID | None = None
    scope: Scope = Field(default_factory=Scope)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    type: Literal["fact", "decision", "solution", "lesson", "convention"] = "fact"
    project: str = Field(default="general", min_length=1, max_length=128)
    tags: list[str] = Field(default_factory=list, max_length=64)
    context: str = Field(default="", max_length=MAX_CONTENT_CHARS)
    importance: Literal["low", "medium", "high", "critical"] = "medium"
    source_format: Literal["auto", "conversation"] = "auto"
    branch: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def validate_tags(self):
        if not self.content.strip():
            raise ValueError("content must be non-empty text")
        if any(len(t) > 128 or t.startswith(("scope:", "team:", "user:")) for t in self.tags):
            raise ValueError("Use scope fields for access; reserved or oversized tag")
        return self


class Search(DTO):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    scope: Scope | None = None
    project: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=10, ge=1, le=MAX_RESULTS)


class RecordRequest(DTO):
    scope: Scope = Field(default_factory=Scope)
    id: int = Field(gt=0)


class History(RecordRequest):
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=MAX_RESULTS)


class Update(RecordRequest):
    request_id: UUID | None = None
    expected_revision: int = Field(gt=0)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_content(self):
        if not self.content.strip():
            raise ValueError("content must be non-empty text")
        return self


class Delete(RecordRequest):
    request_id: UUID | None = None
    expected_revision: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=2000)


class Browse(DTO):
    scope: Scope = Field(default_factory=Scope)
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=MAX_RESULTS)


class Empty(DTO):
    pass


class Work(DTO):
    actor: Actor
    workspace: Workspace
    operation: str
    arguments: dict[str, JsonValue]


class Reply(DTO):
    data: JsonValue = None
    error: str | None = None
    code: str | None = None


IDENTIFIER = r"^[a-zA-Z0-9_-]{1,64}$"
MAX_SECRET_CHARS = 512


class PasswordLogin(DTO):
    user_id: str = Field(pattern=IDENTIFIER)
    password: str = Field(min_length=1, max_length=MAX_SECRET_CHARS)


class TokenLogin(DTO):
    token: str = Field(min_length=1, max_length=MAX_SECRET_CHARS)


class InviteRedeem(DTO):
    user_id: str = Field(pattern=IDENTIFIER)
    code: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=MAX_SECRET_CHARS)


class PasswordChange(DTO):
    current: str = Field(min_length=1, max_length=MAX_SECRET_CHARS)
    new: str = Field(min_length=1, max_length=MAX_SECRET_CHARS)


class MemoryCall(DTO):
    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class TokenCreate(DTO):
    client: str = Field(min_length=1, max_length=128)


class TokenRef(DTO):
    id: str = Field(pattern=r"^[a-f0-9]{64}$")


class UserCreate(DTO):
    id: str = Field(pattern=IDENTIFIER)
    name: str = Field(min_length=1, max_length=128)
    org_role: Literal["member", "company_viewer", "superadmin"] = "member"


class UserRef(DTO):
    user_id: str = Field(pattern=IDENTIFIER)


class UserActive(UserRef):
    active: bool


class OrgRoleChange(UserRef):
    org_role: Literal["member", "company_viewer", "superadmin"]


class TeamRef(DTO):
    team_id: str = Field(pattern=IDENTIFIER)


class TeamCreate(DTO):
    id: str = Field(pattern=IDENTIFIER)
    name: str = Field(min_length=1, max_length=128)


class TeamRename(TeamRef):
    name: str = Field(min_length=1, max_length=128)


class MembershipChange(DTO):
    user_id: str = Field(pattern=IDENTIFIER)
    team_id: str = Field(pattern=IDENTIFIER)
    role: Literal["reader", "editor", "manager"] | None


class TokenFilter(DTO):
    user_id: str | None = Field(default=None, pattern=IDENTIFIER)


class AuditPage(DTO):
    before: int | None = Field(default=None, gt=0)
    limit: int = Field(default=50, ge=1, le=200)
    actor: str | None = Field(default=None, max_length=64)
    action: str | None = Field(default=None, max_length=64)
    subject: str | None = Field(default=None, max_length=128)


class SettingsChange(DTO):
    values: dict[str, str | None] = Field(min_length=1, max_length=64)


class ProviderTest(DTO):
    target: Literal["llm", "embed"]
    provider: str | None = Field(default=None, pattern=r"^[a-z-]{1,32}$")


class SetupVerify(DTO):
    token: str = Field(min_length=1, max_length=64)


class SetupComplete(DTO):
    token: str = Field(min_length=1, max_length=64)
    company_name: str = Field(min_length=1, max_length=128)
    public_url: str | None = Field(default=None, max_length=512)
    user_id: str = Field(pattern=IDENTIFIER)
    name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=MAX_SECRET_CHARS)


class OrganizationUpdate(DTO):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    public_url: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_change(self):
        if self.name is None and self.public_url is None:
            raise ValueError("Provide name or public_url")
        return self
