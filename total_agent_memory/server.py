"""Entry point for pip-installed package. Re-exports from src/server.py."""

import sys
import os
import asyncio

# Add parent directory to path so we can import the actual server
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_server_path = os.path.join(_root, "src", "server.py")

if os.path.exists(_server_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("_server", _server_path)
    _mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_mod)
    main = _mod.main
else:
    async def main():
        print("Error: server.py not found", file=sys.stderr)
        sys.exit(1)


def main_sync():
    """Synchronous entry point for console_scripts."""
    asyncio.run(main())
