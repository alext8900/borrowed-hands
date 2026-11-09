"""Gateway detection (SPEC 6): the listener runs only inside `hermes gateway run`.

register() also runs in `hermes plugins validate`, doctor and CLI processes. There is no public "am I the
gateway" signal, so we require both adjacent argv basenames `gateway run` and `gateway.run` already imported
(verified on Railway). There is deliberately no bypass: the Railway
image sets BH_LISTEN=1, which only *enables* the listener.
"""
from __future__ import annotations

import os
from typing import Mapping, Sequence, Tuple


def should_listen(enabled: bool, argv: Sequence[str], modules: Mapping[str, object]) -> Tuple[bool, str]:
    if not enabled:
        return False, "listen.enabled is false"
    names = [os.path.basename(a) for a in argv]
    argv_ok = any(a == "gateway" and b == "run" for a, b in zip(names, names[1:]))
    mod_ok = "gateway.run" in modules
    if argv_ok and mod_ok:
        return True, "inside hermes gateway run"
    return False, f"not the gateway process (argv gateway run={argv_ok}, gateway.run imported={mod_ok})"
