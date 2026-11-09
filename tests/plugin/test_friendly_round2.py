"""Review round 2 for friendly builds: real-socket deadline and cancellation (F3, N1), a bounded resolver (N2),
and ticket cleanup that never removes another invocation's or session's live ticket (N3, N4, N5).

The transport tests use real sockets (AF_UNIX socketpairs) and real TLS with a generated test certificate, as in
the review's reproductions; nothing touches the network.
"""
import gc
import http.client
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from borrowed_hands import hooks, refs
from test_friendly_build import advertisement, DEMO_REFS

CTYPE = refs.CONTENT_TYPE.encode()


_BASELINE = set()


def _bh_threads():
    return [t for t in threading.enumerate()
            if t.name.startswith("bh-refs") and t.is_alive() and t.ident not in _BASELINE]


def _settle(timeout=1.0):
    end = time.monotonic() + timeout
    while _bh_threads() and time.monotonic() < end:
        time.sleep(0.01)
    return _bh_threads()


@pytest.fixture(autouse=True)
def only_this_tests_workers():
    """Workers left by earlier tests (e.g. a deliberately slow resolver) are not this test's."""
    _BASELINE.clear()
    _BASELINE.update(t.ident for t in threading.enumerate())
    yield
    _settle(3)


def _own(ex, obj):
    (ex.own if hasattr(ex, "own") else ex.append)(obj)


# ---------------------------------------------------------------- F3: cancellation never waits on a reader
def test_cancel_at_deadline_does_not_wait_on_a_late_byte(monkeypatch):
    """Real HTTPResponse over a socketpair: a byte at 250 ms restarts the reader's idle wait; the caller must still
    return at the 300 ms budget, and the worker must stop promptly (the transport is shut down first)."""
    client, server = socket.socketpair()
    client.settimeout(0.3)

    def feed():
        try:
            server.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: " + CTYPE + b"\r\nContent-Length: 32\r\n\r\n")
            time.sleep(0.25)
            server.sendall(b"x")
            time.sleep(0.6)
        finally:
            server.close()
    feeder = threading.Thread(target=feed)
    feeder.start()

    def real_open(url, host, ip, timeout, ex=None):
        _own(ex, client)
        r = http.client.HTTPResponse(client)
        r.begin()
        return r
    monkeypatch.setattr(refs, "TIMEOUT_S", 0.3)
    monkeypatch.setattr(refs, "_getaddrinfo", lambda host: ["140.82.112.3"])
    monkeypatch.setattr(refs, "_open", real_open)
    t0 = time.monotonic()
    with pytest.raises(refs.RefsError) as e:
        refs.fetch_refs("https://granted.example/demo.git")
    took = time.monotonic() - t0
    assert e.value.reason == "refs_unreachable"
    assert took < 0.4, took
    assert _settle(0.2) == [], "the download worker must stop once its transport is shut down"
    assert client.fileno() == -1, "the worker closes what it owns"
    feeder.join(2)


def test_cancel_returns_at_once_and_wakes_a_reader_without_a_timeout():
    """The cancel path on its own (no per-step socket timeout to fall back on): a reader blocked in a real
    HTTPResponse.read1 holds the buffered reader's lock. cancel() must not wait for that lock, and must wake the
    reader by shutting the transport down."""
    client, server = socket.socketpair()
    client.settimeout(2.0)                              # only so a broken cancel cannot hang the suite
    server.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: " + CTYPE + b"\r\nContent-Length: 32\r\n\r\n")
    ex = refs._Exchange(time.monotonic() + 10)
    ex.own(client)
    resp = ex.own(http.client.HTTPResponse(client))
    resp.begin()
    woke = {}

    def reader():
        try:
            resp.read1(65536)
        except Exception as exc:
            woke["exc"] = type(exc).__name__
        woke["at"] = time.monotonic()
    t = threading.Thread(target=reader, name="bh-refs-test-reader")
    t.start()
    time.sleep(0.1)                                     # the reader is now blocked inside read1
    t0 = time.monotonic()
    ex.cancel()
    cancel_took = time.monotonic() - t0
    t.join(3)
    assert cancel_took < 0.05, cancel_took
    assert woke["at"] - t0 < 0.2, woke["at"] - t0
    ex.close_all()
    server.close()


# ---------------------------------------------------------------- N1: TLS is owned before its handshake
@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    d = tmp_path_factory.mktemp("tls")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "granted.example")])
    now = datetime.now(timezone.utc)
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
         .serial_number(1).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
         .add_extension(x509.SubjectAlternativeName([x509.DNSName("granted.example")]), critical=False)
         .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    (d / "cert.pem").write_bytes(c.public_bytes(serialization.Encoding.PEM))
    (d / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
    return d


def tls_peer(monkeypatch, cert, *, connect_delay=0.0, handshake_delay=0.0, body=None):
    """A TLS server on one end of a socketpair; the plugin's TCP connect returns the other end. Records the
    request (and when it arrived) and every client SSL socket the plugin created."""
    client, peer = socket.socketpair()
    seen, made = {}, []

    def serve():
        try:
            time.sleep(handshake_delay)
            sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            sctx.load_cert_chain(str(cert / "cert.pem"), str(cert / "key.pem"))
            with sctx.wrap_socket(peer, server_side=True) as s:
                s.settimeout(2)
                req = b""
                while b"\r\n\r\n" not in req:
                    part = s.recv(4096)
                    if not part:
                        break
                    req += part
                if req:
                    seen["request"], seen["request_at"] = req, time.monotonic()
                    payload = body if body is not None else b""
                    s.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: " + CTYPE + b"\r\nContent-Length: "
                              + str(len(payload)).encode() + b"\r\n\r\n" + payload)
        except Exception as exc:
            seen["server_error"] = type(exc).__name__
        finally:
            peer.close()
    server = threading.Thread(target=serve)
    server.start()

    class Ctx(ssl.SSLContext):
        def wrap_socket(self, *a, **k):
            s = super().wrap_socket(*a, **k)
            made.append(s)
            return s

    def tls_context():
        ctx = Ctx(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=str(cert / "cert.pem"))
        return ctx

    def connect(*a, **k):
        time.sleep(connect_delay)
        ex = a[2] if len(a) > 2 else None
        if ex is not None:
            _own(ex, client)
        return client
    monkeypatch.setattr(refs, "_getaddrinfo", lambda host: ["140.82.112.3"])
    monkeypatch.setattr(refs, "_tls_context", tls_context)
    monkeypatch.setattr(refs, "_tcp_connect", connect, raising=False)
    monkeypatch.setattr(socket, "create_connection", connect)          # the pre-fix code path, kept offline
    return client, server, seen, made


def test_tls_handshake_past_the_deadline_sends_no_request_and_closes(monkeypatch, cert):
    client, server, seen, made = tls_peer(monkeypatch, cert, connect_delay=0.10, handshake_delay=0.25)
    monkeypatch.setattr(refs, "TIMEOUT_S", 0.2)
    t0 = time.monotonic()
    with pytest.raises(refs.RefsError) as e:
        refs.fetch_refs("https://granted.example/demo.git")
    took = time.monotonic() - t0
    server.join(3)
    assert e.value.reason == "refs_unreachable" and took < 0.3, took
    assert "request" not in seen, f"request sent {seen.get('request_at', 0) - t0:.3f}s after start"
    assert _settle(1.0) == []
    gc.collect()
    assert all(s.fileno() == -1 for s in made), "every TLS socket is closed on a timed-out path"


def test_no_request_is_sent_when_the_handshake_ends_after_the_deadline(monkeypatch, cert):
    """The handshake itself succeeds, but only after the budget is spent: the request must not go out."""
    client, server, seen, made = tls_peer(monkeypatch, cert, body=advertisement(DEMO_REFS))
    monkeypatch.setattr(refs, "TIMEOUT_S", 0.3)
    real = ssl.SSLSocket.do_handshake

    def slow_handshake(self, *a, **k):
        out = real(self, *a, **k)
        time.sleep(0.4)                                 # finished, but past the 300 ms budget
        return out
    monkeypatch.setattr(ssl.SSLSocket, "do_handshake", slow_handshake)
    with pytest.raises(refs.RefsError):
        refs.fetch_refs("https://granted.example/demo.git")
    assert _settle(1.5) == []
    server.join(3)
    assert "request" not in seen


def test_tls_success_reads_refs_and_closes_its_socket(monkeypatch, cert):
    client, server, seen, made = tls_peer(monkeypatch, cert, body=advertisement(DEMO_REFS))
    got = refs.fetch_refs("https://granted.example/demo.git")
    server.join(3)
    assert got["refs/heads/bh/fixed"] == "34497174041223af2f6d51c17c85caa4bb6f61a7"
    assert seen["request"].startswith(b"GET /demo.git/info/refs?service=git-upload-pack HTTP/1.1\r\n")
    assert b"Host: granted.example\r\n" in seen["request"]
    assert made and all(s.fileno() == -1 for s in made)


def test_tls_verifies_the_certificate_for_the_name_not_the_address(monkeypatch, cert):
    """The same pinned address, but a name the certificate does not cover: refused, and no request is sent."""
    client, server, seen, made = tls_peer(monkeypatch, cert, body=advertisement(DEMO_REFS))
    with pytest.raises(refs.RefsError) as e:
        refs.fetch_refs("https://wrong.example/demo.git")
    server.join(3)
    assert e.value.reason == "refs_unreachable" and "request" not in seen
    assert all(s.fileno() == -1 for s in made)


# ---------------------------------------------------------------- N2: a stuck resolver is bounded
def test_stuck_resolver_work_is_bounded_and_refused_while_full(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(refs, "TIMEOUT_S", 0.03)
    monkeypatch.setattr(refs, "_getaddrinfo", lambda host: release.wait(5) and ["140.82.112.3"])
    reasons = []
    try:
        for _ in range(refs.MAX_PENDING_LOOKUPS + 3):
            with pytest.raises(refs.RefsError) as e:
                refs.fetch_refs("https://granted.example/demo.git")
            reasons.append(e.value.reason)
        stuck = [t for t in _bh_threads() if t.name == "bh-refs-dns"]
        assert len(stuck) <= refs.MAX_PENDING_LOOKUPS
        assert reasons[:refs.MAX_PENDING_LOOKUPS] == ["refs_unreachable"] * refs.MAX_PENDING_LOOKUPS
        assert reasons[refs.MAX_PENDING_LOOKUPS:] == ["resolver_busy"] * 3
    finally:
        release.set()
    assert _settle(2) == []
    monkeypatch.setattr(refs, "_getaddrinfo", lambda host: ["10.0.0.1"])
    with pytest.raises(refs.RefsError) as e:                          # slots come back once lookups return
        refs.fetch_refs("https://granted.example/demo.git")
    assert e.value.reason == "non_public_address"


# ---------------------------------------------------------------- N3, N4, N5: ticket cleanup
KEY = ("s", "hands_xcode_build", "h")


def test_idless_post_hook_never_removes_an_owned_ticket():
    hooks.TICKETS.clear()
    try:
        hooks.TICKETS.issue(hooks._ticket_key("s", "hands_xcode_build", {}), "mac", "A")
        hooks.post_tool_call(tool_name="hands_xcode_build", args={}, session_id="s", status="blocked",
                             tool_call_id="")
        assert hooks.take("s", "hands_xcode_build", {}, "A") is not None
    finally:
        hooks.TICKETS.clear()


def test_tickets_need_an_owner():
    assert hooks.Tickets().issue(KEY, "mac", "") is None


@pytest.mark.parametrize("bad", ["", "wrong"])
def test_failed_take_at_capacity_keeps_every_live_ticket(bad):
    t = hooks.Tickets()
    for i in range(hooks.MAX_TICKETS):
        t.issue((str(i), "tool", "h"), "mac", f"owner{i}")
    assert t.take(("0", "tool", "h"), bad) is None
    assert t.held_by_other(("1", "tool", "h"), "someone") is True
    assert len(t._t) == hooks.MAX_TICKETS
    assert t.take(("0", "tool", "h"), "owner0") is not None


def test_a_full_store_refuses_the_newcomer_instead_of_evicting_a_live_ticket():
    t = hooks.Tickets()
    for i in range(hooks.MAX_TICKETS):
        t.issue((str(i), "tool", "h"), "mac", f"owner{i}")
    with pytest.raises(hooks.TicketStoreFull):
        t.issue(("new", "tool", "h"), "mac", "newcomer")
    assert all(t.take((str(i), "tool", "h"), f"owner{i}") is not None for i in range(hooks.MAX_TICKETS))


def test_expired_tickets_still_make_room(monkeypatch):
    t = hooks.Tickets()
    for i in range(hooks.MAX_TICKETS):
        t.issue((str(i), "tool", "h"), "mac", f"owner{i}")
    later = time.monotonic() + hooks.TICKET_TTL_S + 1
    monkeypatch.setattr(hooks.time, "monotonic", lambda: later)
    assert t.issue(("new", "tool", "h"), "mac", "newcomer") is not None


def test_hook_answers_busy_when_the_store_is_full(monkeypatch):
    hooks.TICKETS.clear()
    hooks.configure(lambda tool, a: ("mac", "Mac", {**a, "helper_id": "mac"}))
    try:
        for i in range(hooks.MAX_TICKETS):
            hooks.TICKETS.issue((f"x{i}", "tool", "h"), "mac", f"owner{i}")
        d = hooks.pre_tool_call(tool_name="hands_screen_capture", args={"display": 1}, session_id="s",
                                tool_call_id="c")
        assert d["action"] == "block" and "too many" in d["message"].lower()
    finally:
        hooks.TICKETS.clear()
        hooks.configure(lambda tool, a: (a.get("helper_id"), None, dict(a)))


def test_failure_cleanup_is_scoped_to_its_session():
    hooks.TICKETS.clear()
    try:
        for sid in ("one", "two"):
            hooks.TICKETS.issue(hooks._ticket_key(sid, "hands_xcode_build", {}), "mac", "call_0")
        hooks.post_tool_call(tool_name="hands_xcode_build", args={}, session_id="two", status="error",
                             tool_call_id="call_0")
        assert hooks.take("one", "hands_xcode_build", {}, "call_0") is not None
        assert hooks.take("two", "hands_xcode_build", {}, "call_0") is None
    finally:
        hooks.TICKETS.clear()


def test_failure_cleanup_still_finds_a_ticket_whose_args_were_rewritten():
    """Argument middleware may change the args between hook and post hook: cleanup is by session + id."""
    hooks.TICKETS.clear()
    try:
        hooks.TICKETS.issue(hooks._ticket_key("s", "hands_screen_capture", {"display": 1}), "mac", "c")
        hooks.post_tool_call(tool_name="hands_screen_capture", args={"display": 2}, session_id="s",
                             status="blocked", tool_call_id="c")
        assert hooks.take("s", "hands_screen_capture", {"display": 1}, "c") is None
    finally:
        hooks.TICKETS.clear()


# ================================================================ review round 3 (security review)
# ---------------------------------------------------------------- M1: a hook that outlives Hermes's timeout
def test_late_issue_after_a_dropped_invocation_is_refused():
    """Hermes abandons a slow hook worker at plugins.hook_callback_timeout, blocks the call and runs
    post_tool_call(blocked) at once; the worker then reaches issue(). That late ticket must not be stored, or every
    identical call would be refused for the ticket's whole lifetime."""
    hooks.TICKETS.clear()
    started, go = threading.Event(), threading.Event()

    def slow(tool, a):
        started.set()
        go.wait(5)
        return ("mac", "Mac", {**a, "helper_id": "mac"})
    hooks.configure(slow)
    try:
        out = {}
        worker = threading.Thread(target=lambda: out.setdefault("d", hooks.pre_tool_call(
            tool_name="hands_screen_capture", args={"display": 1}, session_id="s", tool_call_id="A")))
        worker.start()
        assert started.wait(2)
        hooks.post_tool_call(tool_name="hands_screen_capture", args={"display": 1}, session_id="s",
                             status="blocked", tool_call_id="A")
        go.set()
        worker.join(5)
        assert hooks.TICKETS._t == {}, "no orphan ticket for an invocation Hermes already finished"
        hooks.configure(lambda tool, a: ("mac", "Mac", {**a, "helper_id": "mac"}))
        d = hooks.pre_tool_call(tool_name="hands_screen_capture", args={"display": 1}, session_id="s",
                                tool_call_id="B")
        assert d["action"] == "approve"
    finally:
        go.set()
        hooks.TICKETS.clear()
        hooks.configure(lambda tool, a: (a.get("helper_id"), None, dict(a)))


def test_a_dead_mark_belongs_to_one_hook_run():
    """Marks are per hook run (a token from begin), not per (session, tool_call_id): a provider's deterministic
    id reused by a later run, even one overlapping the abandoned run, is unaffected."""
    t = hooks.Tickets()
    old = t.begin("one", "B", "turn1")
    t.drop_invocation("one", "B", "turn1")              # Hermes finished B while its hook still runs
    assert t.issue(("one", "tool", "h2"), "mac", "B", token=old) is None
    new = t.begin("one", "B", "turn2")                  # same deterministic id, a later turn, overlapping
    assert t.issue(("one", "tool", "h3"), "mac", "B", token=new) is not None
    t.end(new)
    t.end(old)
    assert t.issue(("one", "tool", "h4"), "mac", "B", token=t.begin("one", "B", "turn3")) is not None
    # both runs live when Hermes finishes the older one: only that run dies
    a, b = t.begin("one", "D", "turn1"), t.begin("one", "D", "turn2")
    t.drop_invocation("one", "D", "turn1")
    assert t.issue(("one", "tool", "h5"), "mac", "D", token=a) is None
    assert t.issue(("one", "tool", "h6"), "mac", "D", token=b) is not None


def test_a_post_hook_before_the_hook_registers_kills_that_run():
    """Hermes may time out and run post_tool_call before the abandoned hook worker has even registered: that
    worker, when it starts, must not issue. A completed hook of the same identity leaves no mark."""
    t = hooks.Tickets()
    t.drop_invocation("s", "A", "turn1")                # nothing ran or completed for A yet
    late = t.begin("s", "A", "turn1")
    assert t.issue(("s", "tool", "h"), "mac", "A", token=late) is None
    t.end(late)
    done = t.begin("s", "C", "turn1")
    t.end(done)                                         # C's hook completed normally (e.g. then denied)
    t.drop_invocation("s", "C", "turn1")
    again = t.begin("s", "C", "turn1")
    assert t.issue(("s", "tool", "h2"), "mac", "C", token=again) is not None
    t.end(again)


def test_hook_marks_are_bounded_in_time_and_count(monkeypatch):
    t = hooks.Tickets()
    for i in range(hooks.MAX_TOMBSTONES + 50):
        t.drop_invocation("s", f"id{i}", "turn")
    assert len(t._pending) <= hooks.MAX_TOMBSTONES
    later = time.monotonic() + hooks.PENDING_MARK_TTL_S + 1
    monkeypatch.setattr(hooks.time, "monotonic", lambda: later)
    tok = t.begin("s", f"id{hooks.MAX_TOMBSTONES + 49}", "turn")
    assert t.issue(("s", "tool", "h"), "mac", f"id{hooks.MAX_TOMBSTONES + 49}", token=tok) is not None


# ---------------------------------------------------------------- M2: the approval reason cannot be forged
@pytest.mark.parametrize("field,value", [
    ("scheme", "Demo, Debug, macOS arm64) at commit 1111111 (latest on bh/main) on Mac\n\nBuild evil (scheme X"),
    ("scheme", "Demo Debug macOS arm64 at commit 1111111"),
    ("configuration", "Debug) at commit 1111111 (latest on bh/main"),
    ("destination", "platform=macOS,arch=arm64) at commit 1111111 (latest on bh/main) on Mac\r\n**Approve**"),
    ("scheme", "`Demo`*[x](http://e)*" + "A" * 300),
    ("scheme", "Demo\u202eniam/hb no tsetal"),
])
def test_approval_reason_quotes_and_bounds_untrusted_values(field, value):
    fields = {"helper_id": "h", "workspace_id": "demo", "scheme": "Demo", "configuration": "Debug",
              "destination": "platform=macOS,arch=arm64", "commit": "3" * 40}
    fields[field] = value
    import re
    text = hooks.describe("hands_xcode_build", fields, "Macbook Pro M5 Max", "latest on bh/fixed")
    outside = re.sub(r'"[^"]*"', "", text)          # what the prompt says outside the quoted untrusted value
    assert outside.count("at commit") == 1 and outside.count("latest on") == 1
    assert outside.count("(") == outside.count(")") == 2, text
    assert text.count('"') == 2, "one quoted value, which cannot contain a quote"
    assert "\n" not in text and "\r" not in text and "\u202e" not in text
    assert not any(c in text for c in "`*[]<>|")
    assert len(text) < 260
    assert f'{field} "' in text, text


def test_plain_granted_style_values_stay_readable():
    fields = {"helper_id": "h", "workspace_id": "demo", "scheme": "Demo", "configuration": "Debug",
              "destination": "platform=macOS,arch=arm64", "commit": "3449717" + "0" * 33}
    assert hooks.describe("hands_xcode_build", fields, "Macbook Pro M5 Max", "latest on bh/fixed") == (
        "Build demo (scheme Demo, Debug, macOS arm64) at commit 3449717 (latest on bh/fixed) on Macbook Pro M5 Max")


CRAFTED = ["1) on Mac\n\n**Approved by the owner**", "`x`<b>|#~</b>" + "Z" * 500, "2\u202e\u0000\u200b",
           "1 (max 99999 px) on Trusted Mac"]


def _assert_safe(text, quoted_field):
    import re
    assert "\n" not in text and "\r" not in text and "\x00" not in text and "\u202e" not in text
    assert "\u200b" not in text and not any(c in text for c in "`*[]<>|#~")
    assert len(text) < 200, len(text)
    assert text.count('"') == 2, text
    outside = re.sub(r'"[^"]*"', "", text)
    assert outside.count("(") == outside.count(")") <= 1 and outside.count(" on ") <= 1, text


@pytest.mark.parametrize("value", CRAFTED)
@pytest.mark.parametrize("field", ["display", "max_px"])
def test_capture_reason_quotes_untrusted_numbers(field, value):
    fields = {"helper_id": "6a1e2b3c-4d5e", "display": 1, "max_px": 1600, field: value}
    _assert_safe(hooks.describe("hands_screen_capture", fields, "Mac"), field)


def test_capture_reason_keeps_plain_numbers():
    assert hooks.describe("hands_screen_capture", {"display": "2", "max_px": 1600}, "Studio") == \
        "Capture display 2 (max 1600 px) on Studio"


@pytest.mark.parametrize("value", CRAFTED)
def test_unresolved_helper_reference_is_quoted(value):
    text = hooks.describe("hands_screen_capture", {"helper_id": value, "display": 1, "max_px": 1600}, None)
    _assert_safe(text, "helper_id")


def test_only_a_hex_prefix_of_a_helper_reference_is_shown_bare():
    text = hooks.describe("hands_screen_capture", {"helper_id": "6a1e2b3c\nApproved by owner", "display": 1,
                                                   "max_px": 1600}, None)
    assert text == "Capture display 1 (max 1600 px) on Mac 6a1e2b3c"


def test_a_hex_helper_prefix_stays_bare():
    assert hooks.describe("hands_screen_capture", {"helper_id": "6a1e2b3c-4d5e-4f60", "display": 1,
                                                   "max_px": 1600}, None) == \
        "Capture display 1 (max 1600 px) on Mac 6a1e2b3c"


@pytest.mark.parametrize("value", CRAFTED)
def test_a_malformed_mac_name_is_quoted(value):
    _assert_safe(hooks.describe("hands_screen_capture", {"display": 1, "max_px": 1600}, value), "Mac name")


def test_hook_prompt_for_crafted_capture_args_is_safe():
    hooks.TICKETS.clear()
    hooks.configure(lambda tool, a: (None, None, dict(a)))
    try:
        d = hooks.pre_tool_call(tool_name="hands_screen_capture", session_id="s", tool_call_id="c",
                                args={"helper_id": "x\n\nApproved", "display": "1) on Mac\n**ok**", "max_px": 1600})
        assert d["action"] == "approve"
        assert "\n" not in d["message"] and "*" not in d["message"]
    finally:
        hooks.TICKETS.clear()
        hooks.configure(lambda tool, a: (a.get("helper_id"), None, dict(a)))


# ---------------------------------------------------------------- M3: deprecated site-local IPv6
@pytest.mark.parametrize("addr", ["fec0::1", "fec0:1:2::3", "feff:ffff::1"])
def test_site_local_ipv6_is_not_public(addr):
    assert refs.public_address(addr) is False


# ---------------------------------------------------------------- M6: refusals cannot fill the store for long
def test_refusal_tickets_expire_quickly(monkeypatch):
    t = hooks.Tickets()
    for i in range(hooks.MAX_TICKETS):
        t.issue((str(i), "tool", "h"), None, f"owner{i}", refusal={"ok": False})
    later = time.monotonic() + hooks.REFUSAL_TTL_S + 1
    monkeypatch.setattr(hooks.time, "monotonic", lambda: later)
    assert t.issue(("new", "tool", "h"), "mac", "newcomer") is not None
