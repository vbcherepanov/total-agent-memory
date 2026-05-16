"""Backward-compat shim: the package was renamed to ``total_agent_memory``.

The legacy import path keeps working so existing user scripts and sub-agent
prompts that do ``from claude_total_memory import ...`` do not break on
upgrade. Every import emits a DeprecationWarning so callers know to switch.

Migration:
    from claude_total_memory import x        # old
    from total_agent_memory import x         # new
"""
from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "The 'claude_total_memory' module has been renamed to 'total_agent_memory'. "
    "Update your imports: `from total_agent_memory import ...`. "
    "The legacy alias will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

from total_agent_memory import *  # noqa: E402, F401, F403
from total_agent_memory import __version__  # noqa: E402, F401
