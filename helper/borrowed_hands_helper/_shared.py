"""Files shared with the rest of the repo: protocol/bhproto.py, protocol/schemas/ and sandbox/.

They are imported, never copied by hand. An installed wheel carries them under `_shared/` (hatch
force-include, see pyproject.toml) with the repo's relative layout; a source checkout reads them from
the repo root. `bhproto` is loaded from exactly one of those two places, the installed copy first.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parent


def _root() -> Path:
    for root in (_PKG / "_shared", _PKG.parents[1]):
        if (root / "protocol" / "bhproto.py").is_file() and (root / "sandbox" / "bh-dispatch").is_file():
            return root
    raise ImportError("borrowed-hands-helper is missing protocol/bhproto.py or sandbox/; reinstall it")


ROOT = _root()
SCHEMAS = ROOT / "protocol" / "schemas"
SANDBOX = ROOT / "sandbox"          # the `<share>/sandbox` of SPEC 8.4


def _load_bhproto():
    name = "borrowed_hands_helper.bhproto"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "protocol" / "bhproto.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bhproto = _load_bhproto()
