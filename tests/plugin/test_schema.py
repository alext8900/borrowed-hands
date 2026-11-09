"""PR-6: the runtime schema checker against every schema in protocol/schemas, with PROTOCOL.md examples."""
import json
from pathlib import Path

import pytest

from borrowed_hands import schema

ROOT = Path(__file__).resolve().parents[2]
U1 = "3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b"
U2 = "6a1e2b3c-4d5e-4f60-8172-839405a6b7c8"
SIG = "A" * 86
PUB = "A" * 43
FP = "c" * 64

BUILD_RESULT = {"status": "failed", "duration_ms": 10590, "xcode": "26.1 (17B55)",
                "destination": "platform=macOS,arch=arm64", "error_count": 1, "warning_count": 0,
                "errors": [{"file": "Sources/Greeting.swift", "line": 1, "column": 42,
                            "message": "Cannot find 'undefinedThing' in scope", "type": "Swift Compiler Error"}],
                "warnings": [], "log_tail": "x", "truncated": False}
INFO = {"os_version": "26.2", "os_build": "25C56", "arch": "arm64", "cpu": "Apple M4", "memory_gb": 16,
        "xcode": {"version": "26.1", "build": "17B55"}, "disk_free_gb": 100}
# PROTOCOL 6.1 shows "Alex's MacBook Pro", but the frozen NAME pattern has no apostrophe (reported as a spec
# bug); the helper must send a name inside the pattern.
CAPS = {"helper_version": "0.1.0", "name": "Alex MacBook Pro", "os": "macos", "os_version": "26.2",
        "arch": "arm64", "busy": False, "queue_depth": 0,
        "capabilities": [{"name": "system.info", "v": 1}, {"name": "screen.capture", "v": 1, "displays": 1},
                         {"name": "xcode.build", "v": 1, "xcode": "26.1", "xcode_build": "17B55",
                          "workspaces": [{"workspace_id": "demo", "repo_fingerprint": FP, "schemes": ["Demo"],
                                          "configurations": ["Debug"],
                                          "destinations": ["platform=macOS,arch=arm64"], "expires_at": None}]}]}
CONTRACT = {"v": 1, "job_id": U1, "nonce": "AAECAwQFBgcICQoLDA0ODw", "helper_id": U2,
            "server_key_id": "56475aa75463474c", "action": "xcode.build",
            "params": {"workspace_id": "demo", "repo_fingerprint": FP, "commit": "5" * 40, "scheme": "Demo",
                       "configuration": "Debug", "destination": "platform=macOS,arch=arm64"},
            "issued_at": 1793861063000, "expires_at": 1793861663000, "approval_ref": "hermes:" + U1}

GOOD_PAIR = {"code": "AAECAwQFBgcICQoLDA0ODw", "device_pubkey": PUB, "device_name": "Alex Mac mini",
             "client_nonce": PUB, "helper_version": "0.1.0", "os": "macos", "os_version": "26.2",
             "arch": "arm64", "pair_sig": SIG}

GOOD = {
    "envelope.json": [{"v": 1, "p": "e30"}, {"v": 1, "p": "e30", "s": SIG}],
    "payload.json": [{"type": "job.submit", "id": U1, "re": U2, "sid": U2, "seq": 7, "ts": 1, "exp": 2, "body": {}}],
    "payload-handshake.json": [{"type": "hello", "id": U1, "ts": 1, "body": {}}],
    "error.json": [{"code": c, "message": "m"} for c in schema.ERROR_CODES]
                  + [{"code": "helper_busy", "message": "queue full", "detail": {"reason": "queue_full"},
                      "retry_after_s": 30}],
    "hello.json": [{"v": 1, "server_nonce": PUB, "server_key_id": "56475aa75463474c", "server_time_ms": 1,
                    "max_frame": 16777216}],
    "auth.json": [{"helper_id": U2, "client_nonce": PUB, "device_sig": SIG}],
    "auth-ok.json": [{"sid": U1, "server_sig": SIG, "server_time_ms": 1}],
    "pair.json": [{"code": "AAECAwQFBgcICQoLDA0ODw", "device_pubkey": PUB, "device_name": "Alex Mac mini",
                   "client_nonce": PUB, "helper_version": "0.1.0", "os": "macos", "os_version": "26.2",
                   "arch": "arm64", "pair_sig": SIG}],
    "pair-ok.json": [{"helper_id": U2, "sid": U1, "server_sig": SIG}],
    "caps.json": [CAPS],
    "contract.json": [CONTRACT,
                      {**CONTRACT, "action": "system.info", "params": {}, "approval_ref": "none"},
                      {**CONTRACT, "action": "screen.capture", "params": {"display": 1, "max_px": 1600}}],
    "params-xcode-build.json": [CONTRACT["params"]],
    "params-screen-capture.json": [{"display": 1, "max_px": 1600}],
    "params-system-info.json": [{}],
    "job-submit.json": [{"contract": "e30", "sig": SIG}],
    "job-state.json": [{"job_id": U1, "state": "queued", "at": 1, "position": 0}],
    "job-cancel.json": [{"job_id": U1}],
    "job-ack.json": [{"job_ids": [U1, U2]}],
    "job-result.json": [
        {"job_id": U1, "state": "failed", "result": BUILD_RESULT, "error": None},
        {"job_id": U1, "state": "succeeded", "result": INFO, "error": None},
        {"job_id": U1, "state": "succeeded",
         "result": {"mime": "image/png", "width": 10, "height": 10, "display": 1, "png_b64": "iVBO"}, "error": None},
        {"job_id": U1, "state": "failed", "result": None,
         "error": {"code": "timeout", "message": "build exceeded 900 s"}},
        {"job_id": U1, "state": "succeeded", "result": {**INFO, "xcode": None}, "error": None}],
    "jobs-sync.json": [{"active": [{"job_id": U1, "state": "building"}],
                        "results": [{"job_id": U2, "state": "cancelled", "result": None,
                                     "error": {"code": "cancelled", "message": "m"}}]}],
    "device-rotate.json": [{"new_pubkey": PUB, "sig_new": SIG}],
    "server-rotate.json": [{"new_pubkey": PUB, "new_key_id": "56475aa75463474c", "sig_new": SIG}],
    "unpaired.json": [{"reason": "unpaired by owner"}],
    "empty.json": [{}],
}

BAD = {
    "pair.json": [{**GOOD_PAIR, "device_name": "Alex's Mac mini"}],  # frozen pattern: no apostrophe
    "payload.json": [
        {"type": "job.submit\n", "id": U1, "sid": U2, "seq": 7, "ts": 1, "exp": 2, "body": {}},  # $ vs newline
        {"type": "job.submit", "id": U1, "sid": U2, "seq": 0, "ts": 1, "exp": 2, "body": {}},
        {"type": "job.submit", "id": U1, "sid": U2, "seq": True, "ts": 1, "exp": 2, "body": {}},
        {"type": "job.submit", "id": "not-a-uuid", "sid": U2, "seq": 1, "ts": 1, "exp": 2, "body": {}},
        {"type": "job.submit", "id": U1, "sid": U2, "seq": 1, "ts": 1, "exp": 2, "body": {}, "x": 1},
        {"type": "job.submit", "id": U1, "seq": 1, "ts": 1, "exp": 2, "body": {}},
    ],
    "error.json": [{"code": "nope", "message": "m"}, {"code": "denied", "message": "x" * 1001}],
    "contract.json": [
        {**CONTRACT, "params": {**CONTRACT["params"], "url": "https://evil"}},           # unknown param
        {**CONTRACT, "params": {**CONTRACT["params"], "commit": "main"}},
        {**CONTRACT, "action": "system.info"},                                           # params mismatch
        {**CONTRACT, "action": "shell.exec"},
        {**CONTRACT, "approval_ref": "hermes:x"},
    ],
    "caps.json": [{**CAPS, "capabilities": [{"name": "system.info", "v": 1, "extra": 1}]},
                  {**CAPS, "capabilities": [{"name": "xcode.build", "v": 1}]}],
    "job-result.json": [
        {"job_id": U1, "state": "failed",
         "result": {**BUILD_RESULT, "errors": [{**BUILD_RESULT["errors"][0], "file": "/Users/alex/x.swift"}]},
         "error": None},
        {"job_id": U1, "state": "failed", "result": {**BUILD_RESULT, "errors": [BUILD_RESULT["errors"][0]] * 51},
         "error": None},
        {"job_id": U1, "state": "failed", "result": {**BUILD_RESULT, "log_tail": "x" * 4001}, "error": None},
        {"job_id": U1, "state": "queued", "result": None, "error": None},
    ],
    "hello.json": [{"v": 1, "server_nonce": PUB, "server_key_id": "56475aa75463474c", "server_time_ms": 1.5,
                    "max_frame": 16777216}],
}


def test_every_protocol_schema_has_good_examples_and_copy_matches():
    names = sorted(p.name for p in (ROOT / "protocol" / "schemas").glob("*.json"))
    assert sorted(GOOD) == names
    for n in names:
        assert (ROOT / "protocol" / "schemas" / n).read_bytes() == (ROOT / "borrowed-hands" / "schemas" / n).read_bytes()


@pytest.mark.parametrize("name,doc", [(n, d) for n, ds in GOOD.items() for d in ds])
def test_good_examples_validate(name, doc):
    assert schema.errors(name, doc) == []


@pytest.mark.parametrize("name,doc", [(n, d) for n, ds in BAD.items() for d in ds])
def test_bad_examples_fail(name, doc):
    assert schema.errors(name, doc) != []


def test_error_enum_includes_helper_busy():
    assert "helper_busy" in schema.ERROR_CODES
    doc = json.loads((ROOT / "protocol" / "schemas" / "error.json").read_text())
    assert doc["properties"]["code"]["enum"] == list(schema.ERROR_CODES)


def test_unknown_schema_name_raises():
    with pytest.raises(KeyError):
        schema.errors("nope.json", {})


def test_one_of_needs_exactly_one_match():
    node = {"oneOf": [{"type": "integer"}, {"type": "integer", "minimum": 0}]}
    assert schema._sub_errors(node, 5, "$") != []      # matches both branches
    assert schema._sub_errors(node, -5, "$") == []     # matches exactly one
    assert schema._sub_errors(node, "x", "$") != []    # matches none
