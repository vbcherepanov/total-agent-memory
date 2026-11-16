from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

SENSITIVE_NAMES = ('.env', '.env.*', '*.pem', '*.key', 'id_rsa*', 'id_ed25519*', '.npmrc', '.pypirc')
SAFE_ENV_NAMES = frozenset({'.env.example', '.env.sample', '.env.template'})


def sensitive(path: str) -> bool:
    name = PurePosixPath(path.replace('\\', '/')).name
    return name not in SAFE_ENV_NAMES and any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_NAMES)
