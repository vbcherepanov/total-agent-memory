"""Claude Total Memory — package re-export for importability."""

import os
import sys

# When running from cloned repo, allow importing the actual server
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_server_path = os.path.join(_root, "src", "server.py")

if os.path.exists(_server_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("_server", _server_path)
    _mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_mod)
    Store = _mod.Store
    Recall = _mod.Recall
    main = _mod.main
