"""The plugin's one runtime object per process, built by register() and read by tools, hooks and commands.

`RT.degraded` holds why the plugin cannot reach Macs (no store, no key); tools then answer helper_unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Settings


@dataclass
class Runtime:
    settings: Settings
    data_dir: Optional[Path] = None
    store: Any = None
    key: Any = None
    listener: Any = None
    jobs: Any = None
    notifier: Any = None
    ctx: Any = None
    degraded: Optional[str] = None
    gate_reason: str = ""
    pid: int = 0

    # ------------------------------------------------------------ helper resolution (SPEC 5)
    def helper_caps(self, h) -> List[str]:
        caps = (h.caps or {}).get("capabilities") or []
        return [c.get("name") for c in caps if isinstance(c, dict)]

    def online(self, helper_id: str) -> bool:
        return self.listener is not None and self.listener.running and self.listener.is_online(helper_id)

    def resolve_helper(self, ref: Optional[str], capability: str) -> Dict[str, Any]:
        """{"helper": Helper} or {"error": (code, message, detail)}."""
        if self.store is None:
            return {"error": ("helper_unavailable", "Borrowed Hands is not set up on this Hermes.",
                              {"reason": "plugin_degraded"})}
        if ref:
            h = self.store.find_helper(ref)
            if h is None or h.revoked_at is not None:
                return {"error": ("invalid_arguments", "No paired Mac with that helper_id or name.",
                                  {"reason": "unknown_helper"})}
            if capability not in self.helper_caps(h):
                return {"error": ("helper_unavailable", f"{h.name} has not enabled {capability}. Its owner can run "
                                  f"`bh caps enable {capability}` on the Mac.", {"reason": "capability_disabled"})}
            return {"helper": h}
        live = [h for h in self.store.list_helpers() if h.revoked_at is None]
        fits = [h for h in live if self.online(h.helper_id) and capability in self.helper_caps(h)]
        if len(fits) == 1:
            return {"helper": fits[0]}
        if not live:
            return {"error": ("helper_unavailable", "No Mac is paired. The owner can run /hands pair.",
                              {"reason": "no_helpers"})}
        cands = [{"helper_id": h.helper_id, "name": h.name, "online": self.online(h.helper_id)} for h in live]
        if not fits:
            return {"error": ("helper_unavailable", f"No online Mac offers {capability} right now.",
                              {"reason": "no_matching_helper", "candidates": cands})}
        return {"error": ("invalid_arguments", "More than one Mac matches; pass helper_id.",
                          {"reason": "ambiguous_helper", "candidates": cands})}


RT: Optional[Runtime] = None
