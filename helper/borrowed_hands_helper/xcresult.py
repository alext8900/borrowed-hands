"""Turn `xcresulttool get build-results` JSON into a small, structurally safe diagnostics report.

What the scrubber is, and is not
--------------------------------
It is NOT a security boundary against attacker-crafted text. The attacker controls the repo, so
anything they write into source or `#line` directives is a string they already know; chasing
every Unicode or encoding variant of a path they typed is unwinnable. Prompt-injection safety
comes from the plugin labelling diagnostics as untrusted data, not from this module.

Its job is:
  (a) keep REAL machine metadata emitted by the toolchain out of results: the builder home
      (/Users/Shared/.bh-builder-home), the job dir, DerivedData, /private/var/folders temp and
      cache dirs, the real username. Pass the known ones as `sensitive_roots`; the pattern
      redaction (/Users/*, /private/var/*, ~/..., /Volumes/*) is the safety net.
  (b) keep every output field structurally safe: no control, format or line-separator
      characters, capped lengths, capped counts.

Accepted gaps, by design: homoglyphs and combining marks inside names, and races with a
same-user concurrent writer (TOCTOU) in the filesystem checks done elsewhere.
Deviation worth knowing: the /Users, /Volumes and /private/var patterns match anywhere except
right after a word character, so `Sources/Users/List.swift` is left alone but `../Users/x` is not.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

MAX_MESSAGE_LEN = 500
MAX_DIAGNOSTICS = 50
XCRESULTTOOL_TIMEOUT = 60.0
MAX_FILE_LEN = 200
MAX_ISSUE_TYPE_LEN = 80
MAX_STATUS_LEN = 40
MAX_COUNT = 10_000_000
MAX_DECODE_ROUNDS = 8
REDACTED = "<redacted>"
PLACEHOLDER = "<path>"
_INVISIBLE = ("Cc", "Cf", "Zl", "Zp")
_SEG = r"[^\s'\":]"
# an absolute-looking span, normalised before matching (not the tail of a scheme like https://)
_SPAN_RE = re.compile(rf"(?<![:\w.~-])/{_SEG}+")
_FILE_HOST_RE = re.compile(r"file://[^/\s'\"]*", re.I)  # file://host/Users/x -> /Users/x
# A redaction root may start at: start of field, whitespace, a quote, = : ( [ ,  or right after a
# compiler flag (-L/path, -isystem/path) or ../ . Not after a word char: Sources/Users/List.swift stays.
_BOUNDARY = r"(?<![^\s'\"=:(\[,])"
_FLAG = r"-[A-Za-z]{1,10}|\.{1,2}"
# Once a root starts, redact to the next delimiter (paths contain spaces): a quote, ) or ],
# comma+space, " (", or end of field. Trailing text is lost on purpose.
_TAIL = r"(?:(?!['\")\]]|, | \().)*"
_GENERIC_ROOTS = (r"/System/Volumes/Data/|/Volumes/|/Users/|/private/|/var/folders/|/tmp/|/etc/"
                  r"|~[\w.-]*/")
_GENERIC_RE = re.compile(rf"{_BOUNDARY}(?P<pre>{_FLAG})?(?:{_GENERIC_ROOTS}){_TAIL}", re.I)
_ALLOWED_PUNCT = set("._-+@/()[], ")


class XcresultError(Exception):
    pass


@dataclass
class Diagnostic:
    severity: str  # "error" | "warning"
    message: str
    file: str | None  # relative to source root, else basename, else None
    line: int | None  # 1-based
    column: int | None  # 1-based
    issue_type: str | None


@dataclass
class BuildDiagnostics:
    status: str
    error_count: int
    warning_count: int
    diagnostics: list[Diagnostic] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Ctx:
    roots: list[str]                    # source root spellings (stripped from paths)
    sensitive_re: re.Pattern[str] | None  # known real machine paths, redacted wherever they appear


def _variants(path: str | os.PathLike[str]) -> list[str]:
    raw = posixpath.normpath(re.sub(r"/+", "/", os.fspath(path)))  # collapse //, /./, ..
    real = os.path.realpath(raw)
    out = [raw, real]
    for v in (raw, real):  # macOS: /var, /tmp, /etc are symlinks into /private
        out.append(v[len("/private"):] if v.startswith("/private/") else "/private" + v)
    return list(dict.fromkeys(out))


def _make_ctx(source_root: Any, sensitive_roots: Any) -> _Ctx:
    roots = _variants(source_root) if source_root is not None else []
    sens: list[str] = []
    for s in sensitive_roots or []:
        forms = [s, _decode_fixed_point(s) or s]
        for f in forms:
            for v in _variants(f):
                v = v.rstrip("/")
                # the source root is stripped (diagnostics stay useful), not blanked
                if v and v not in roots and v not in sens:
                    sens.append(v)
    pat = None
    if sens:
        alts = "|".join(re.escape(r) for r in sorted(sens, key=len, reverse=True))
        pat = re.compile(rf"(?:{alts}){_TAIL}", re.I)
    return _Ctx(roots, pat)


def _strip_invisible(text: str) -> str:
    """Control/format/line-separator chars out (newline, CR, tab become a space)."""
    return "".join(
        " " if c in "\n\r\t" else c for c in text if c in "\n\r\t" or unicodedata.category(c) not in _INVISIBLE
    )


def _decode_fixed_point(text: str) -> str | None:
    """Strip invisibles and percent-decode until stable. None if it never settles."""
    text = _strip_invisible(text)
    for _ in range(MAX_DECODE_ROUNDS):
        decoded = _strip_invisible(unquote(text))
        if decoded == text:
            return None if re.search(r"%[0-9A-Fa-f]{2}", text) else text
        text = decoded
    return None


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "\N{HORIZONTAL ELLIPSIS}"


def _strip_root_span(m: re.Match[str], ctx: _Ctx) -> str:
    span = posixpath.normpath(re.sub(r"/+", "/", m.group(0)))
    for r in sorted(ctx.roots, key=len, reverse=True):  # anchored: only at the start of a span
        if span.startswith(r.rstrip("/") + "/"):
            return span[len(r.rstrip("/")) + 1:]
    return span


def _scrub(text: str, ctx: _Ctx, limit: int) -> str:
    """Invisibles and encodings first (so nothing hides a path), then normalise path spans and
    strip the source root, then redact known real paths, then generic home/system roots."""
    decoded = _decode_fixed_point(text)
    if decoded is None:
        return REDACTED
    text = _FILE_HOST_RE.sub("", decoded)  # before redaction, so it can never eat a placeholder
    text = _SPAN_RE.sub(lambda m: _strip_root_span(m, ctx), text)
    if ctx.sensitive_re:
        text = ctx.sensitive_re.sub(PLACEHOLDER, text)
    text = _GENERIC_RE.sub(lambda m: (m.group("pre") or "") + PLACEHOLDER, text)
    return _cap(_strip_invisible(text), limit)


def _allowed(text: str) -> bool:
    """Letters, numbers, combining marks, and a small punctuation set; nothing else."""
    return all(c in _ALLOWED_PUNCT or unicodedata.category(c)[0] in "LNM" for c in text)


def _file_field(url_path: str, ctx: _Ctx) -> str:
    """Correctness path for `file`: decode once (to a fixed point), NFKC, normalise, decide, and
    only ever emit allowlisted characters. Under the source root -> relative path; otherwise
    the basename; otherwise <unknown>."""
    decoded = _decode_fixed_point(url_path)
    if decoded is None:
        return REDACTED
    t = posixpath.normpath(re.sub(r"/+", "/", unicodedata.normalize("NFKC", decoded)))
    out = None
    if t.startswith("/"):
        for r in ctx.roots:
            prefix = r.rstrip("/") + "/"
            if t.startswith(prefix):
                out = t[len(prefix):]
                break
    elif t not in (".", ""):
        out = t
    if out is None or out.startswith("/") or ".." in out.split("/") or not _allowed(out):
        out = posixpath.basename(t)
        if out in ("", ".", "..") or not _allowed(out):
            return "<unknown>"
    return _cap(out, MAX_FILE_LEN)


def _positive_int(values: list[str] | None, zero_based: bool) -> int | None:
    if not values:
        return None
    try:
        n = int(values[0])  # first occurrence wins if a key repeats
    except ValueError:
        return None
    if n < 0 or n > MAX_COUNT:
        return None
    return n + 1 if zero_based else n


def _location(source_url: Any, ctx: _Ctx) -> tuple[str | None, int | None, int | None]:
    if not isinstance(source_url, str) or not source_url:
        return None, None, None
    parts = urlsplit(source_url)
    file = _file_field(parts.path, ctx) if parts.path else None
    frag = parse_qs(parts.fragment)
    # Xcode fragments are 0-based; report 1-based like editors and compilers do.
    return (file, _positive_int(frag.get("StartingLineNumber"), True),
            _positive_int(frag.get("StartingColumnNumber"), True))


def parse_build_results(
    json_obj: Any,
    source_root: str | os.PathLike[str] | None = None,
    max_message_len: int = MAX_MESSAGE_LEN,
    max_diagnostics: int = MAX_DIAGNOSTICS,
    sensitive_roots: list[str] | None = None,
) -> BuildDiagnostics:
    if not isinstance(json_obj, dict):
        raise XcresultError("build-results JSON must be an object")
    ctx = _make_ctx(source_root, sensitive_roots)
    items: list[Diagnostic] = []
    total = 0
    for key in ("errors", "warnings"):
        if json_obj.get(key) is not None and not isinstance(json_obj[key], list):
            raise XcresultError(f"{key} must be a list")
    for key, severity in (("errors", "error"), ("warnings", "warning")):
        for raw in json_obj.get(key) or []:
            total += 1
            if len(items) >= max_diagnostics or not isinstance(raw, dict):
                continue
            file, line, col = _location(raw.get("sourceURL"), ctx)
            items.append(Diagnostic(
                severity=severity,
                message=_scrub(str(raw.get("message", "")), ctx, max_message_len),
                file=file, line=line, column=col,
                issue_type=(_scrub(raw["issueType"], ctx, MAX_ISSUE_TYPE_LEN)
                            if isinstance(raw.get("issueType"), str) else None),
            ))

    def count(key: str, listed: str) -> int:
        v = json_obj.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            return min(v, MAX_COUNT)
        return min(len(json_obj.get(listed) or []), MAX_COUNT)

    return BuildDiagnostics(
        status=_scrub(str(json_obj.get("status", "unknown")), ctx, MAX_STATUS_LEN),
        error_count=count("errorCount", "errors"),
        warning_count=count("warningCount", "warnings"),
        diagnostics=items,
        truncated=total > len(items),
    )


def from_bundle(path: str | os.PathLike[str], source_root: str | os.PathLike[str] | None = None,
                timeout: float = XCRESULTTOOL_TIMEOUT, **kw: Any) -> BuildDiagnostics:
    argv = ["xcrun", "xcresulttool", "get", "build-results", "--path", os.fspath(Path(path))]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired as e:
        raise XcresultError(f"xcresulttool timed out after {timeout}s") from e
    except OSError as e:
        raise XcresultError(f"cannot run xcresulttool: {e}") from e
    if r.returncode != 0:
        raise XcresultError(f"xcresulttool failed: {r.stderr.strip()[:300]}")
    try:
        obj = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise XcresultError("xcresulttool returned invalid JSON") from e
    return parse_build_results(obj, source_root=source_root, **kw)
