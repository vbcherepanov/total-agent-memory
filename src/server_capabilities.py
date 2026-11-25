"""Capabilities the personal and the team server advertise on /healthz.

The client side of the check lives in team_memory.remote, which must stay a single
standard-library file that can be copied to a client machine on its own.
"""
from team_memory.remote import (
    CAPABILITY_VERSION,
    HTTP_AUTH_REJECTED,
    HTTP_NOT_FOUND,
    MAX_HEALTH_BYTES,
    PREFLIGHT_TIMEOUT_SECONDS,
    UNREACHABLE_EXIT,
    UNSUPPORTED_EXIT,
    CapabilityFailure,
    require_capabilities,
    required_capabilities,
)

__all__ = ["CAPABILITY_VERSION", "HTTP_AUTH_REJECTED", "HTTP_NOT_FOUND", "MAX_HEALTH_BYTES", "PERSONAL_CAPABILITIES",
           "PREFLIGHT_TIMEOUT_SECONDS", "TEAM_CAPABILITIES", "UNREACHABLE_EXIT", "UNSUPPORTED_EXIT", "CapabilityFailure",
           "require_capabilities", "required_capabilities"]

PERSONAL_CAPABILITIES = ("recall.snapshot", "recall.evidence", "records.source_format", "records.embedding_space",
                         "records.branch", "records.incidents", "export.snapshot")
TEAM_CAPABILITIES = ("records.source_format", "records.branch", "writes.request_id", "writes.expected_revision", "endpoint.readonly")
