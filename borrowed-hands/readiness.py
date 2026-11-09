"""Bounded owner-facing summaries of advertised usable capabilities. No grant inference."""
from __future__ import annotations

import re

MAX_TEXT = 1800


def _label(value):
    # Only protocol labels, never paths, free text or Markdown supplied by a helper. Escape what the label charset
    # can still mean in Markdown (underscore emphasis, a leading "1." list marker) so it renders literally.
    if not (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9 ._+-]{0,63}', value)):
        return None
    return re.sub(r'^([0-9]+)\.', r'\1\\.', value.replace('_', r'\_'))


def _destination(value):
    if value == 'platform=macOS,arch=arm64':
        return value.replace('platform=', '').replace(',arch=', ' ')
    if value == 'generic/platform=iOS Simulator':
        return value.removeprefix('generic/platform=') + ' (generic)'
    return None


def _listed(values):
    return ", ".join(values[:3]) + (f" (and {len(values) - 3} more)" if len(values) > 3 else "")


def summary(name, caps, now, allowed):
    """Omit unusable, expired and unknown rows; bound output without truncating a grant into a new meaning."""
    details, seen_caps = set(), set()
    for cap in caps.get('capabilities', []):
        kind = cap.get('name')
        if kind not in allowed or kind in seen_caps:
            continue
        seen_caps.add(kind)
        if kind == 'system.info':
            details.add('System information.')
        elif kind == 'screen.capture' and cap.get('displays', 0) > 0:
            details.add('Screen capture.')
        elif kind == 'xcode.build':
            seen_workspaces = set()
            for ws in cap.get('workspaces', []):
                workspace_id = ws.get('workspace_id')
                if workspace_id in seen_workspaces:
                    continue
                seen_workspaces.add(workspace_id)
                expires = ws.get('expires_at')
                if expires is not None and expires <= now:
                    continue
                workspace = _label(ws.get('workspace_id'))
                schemes = sorted(set(filter(None, map(_label, ws.get('schemes', [])))))
                configs = sorted(set(filter(None, map(_label, ws.get('configurations', [])))))
                destinations = sorted(set(filter(None, map(_destination, ws.get('destinations', [])))))
                if workspace and schemes and configs and destinations:
                    details.add('Xcode builds: ' + ' / '.join((workspace, _listed(schemes),
                                _listed(configs), _listed(destinations))) + '.')
    if not details:
        return None
    text = (_label(name) or 'Your Mac') + ' is ready.'
    for detail in sorted(details):
        if len(text) + len(detail) + 1 > MAX_TEXT - 70:
            text += ' More advertised capabilities are available in /hands status.'
            break
        text += ' ' + detail
    # A single huge workspace row must never produce a bare, unsupported "ready" claim.
    return text if any(detail in text for detail in details) else None
