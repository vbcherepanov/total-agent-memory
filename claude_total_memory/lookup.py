"""Backward-compat shim. Real entry-point: ``total_agent_memory.lookup``.

Replaces this module in ``sys.modules`` with the real implementation so
every attribute (including private helpers like ``_fts_query``) resolves
transparently. This keeps third-party tests and sub-agent scripts that do
``import claude_total_memory.lookup as lookup`` working.
"""
import sys as _sys

from total_agent_memory import lookup as _impl

_sys.modules[__name__] = _impl
