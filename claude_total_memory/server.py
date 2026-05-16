"""Backward-compat shim. Real entry-point: ``total_agent_memory.server``."""
import sys as _sys

from total_agent_memory import server as _impl

_sys.modules[__name__] = _impl
