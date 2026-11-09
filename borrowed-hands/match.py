"""Bounded requirement parser and evaluator (PROTOCOL 6.2).

Caps are schema-validated, signed snapshots from the listener. Destination requirements use
`destination=<exact destination>` ; no wire messages are added.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

CAPABILITIES = ("system.info", "screen.capture", "xcode.build")
DESTINATIONS = ("platform=macOS,arch=arm64", "generic/platform=iOS Simulator")
VERSION_REQ = re.compile(r"(os_version|xcode)(>=|=)([0-9]+(?:\.[0-9]+){0,3})")
WORD_REQ = re.compile(r"(os|arch)=([A-Za-z0-9_]+)")


class InvalidRequirement(ValueError):
    def __init__(self, text):
        # Echo short offending strings only; never copy unbounded model input into the result.
        if isinstance(text, str):
            self.text = text if len(text) <= 64 else f"<{len(text)} chars>"
        else:
            self.text = f"<{type(text).__name__}>"
        super().__init__(f"Invalid requirement: {self.text}")


@dataclass(frozen=True)
class Req:
    text: str
    key: str
    op: str
    value: str


def parse(reqs: list[str]) -> list[Req]:
    if not isinstance(reqs, list):
        raise InvalidRequirement(reqs)
    if len(reqs) > 8:
        raise InvalidRequirement(reqs[8])
    out = []
    for text in reqs:
        if not isinstance(text, str) or len(text) > 64:
            raise InvalidRequirement(text)
        if text in CAPABILITIES:
            out.append(Req(text, "capability", "=", text))
        elif text.startswith("destination=") and text[len("destination="):] in DESTINATIONS:
            out.append(Req(text, "destination", "=", text[len("destination="):]))
        elif m := VERSION_REQ.fullmatch(text):
            out.append(Req(text, *m.groups()))
        elif m := WORD_REQ.fullmatch(text):
            out.append(Req(text, m[1], "=", m[2].casefold()))
        else:
            raise InvalidRequirement(text)
    return out


def _version(text: str) -> tuple[int, ...] | None:
    """Integer tuple padded to 4, or None for anything unparsable (never raises). Leading zeros are
    insignificant; Python's int() digit limit turns absurd lengths into ValueError -> None."""
    try:
        parts = text.split(".")
        if not 1 <= len(parts) <= 4 or any(not p.isascii() or not p.isdigit() for p in parts):
            return None
        nums = tuple(int(p.lstrip("0") or "0") for p in parts)
    except (AttributeError, ValueError):
        return None
    return nums + (0,) * (4 - len(nums))


def evaluate(reqs: list[Req], caps: dict) -> list[str]:
    """Return the original text of each unmet requirement, in request order (AND)."""
    capabilities = {}
    for cap in caps.get("capabilities", []):
        capabilities.setdefault(cap["name"], cap)  # first entry wins, as in the tool's row
    xcode = capabilities.get("xcode.build", {})
    unmet = []
    for req in reqs:
        if req.key == "capability":
            matched = req.value in capabilities
        elif req.key == "destination":
            matched = any(req.value in ws["destinations"] for ws in xcode.get("workspaces", []))
        elif req.key in ("os", "arch"):
            matched = caps.get(req.key, "").casefold() == req.value
        else:
            actual = xcode.get("xcode") if req.key == "xcode" else caps.get("os_version")
            have, want = _version(actual) if actual is not None else None, _version(req.value)
            if have is None or want is None:
                matched = False
            elif req.op == ">=":
                matched = have >= want
            else:
                matched = have == want
        if not matched:
            unmet.append(req.text)
    return unmet
