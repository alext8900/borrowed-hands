"""The helper's minimal validator over protocol/schemas (the authoritative wire shapes)."""
from __future__ import annotations

import pytest

from borrowed_hands_helper import schema
from borrowed_hands_helper.schema import SchemaError

UUID = "0b9f3c2e-1d4a-4c5b-8e6f-7a8b9c0d1e2f"


def contract(**kw):
    c = {"v": 1, "job_id": UUID, "nonce": "A" * 22, "helper_id": UUID, "server_key_id": "0123456789abcdef",
         "action": "xcode.build",
         "params": {"workspace_id": "demo", "repo_fingerprint": "a" * 64, "commit": "b" * 40,
                    "scheme": "Demo", "configuration": "Debug", "destination": "platform=macOS,arch=arm64"},
         "issued_at": 1, "expires_at": 2, "approval_ref": "none"}
    c.update(kw)
    return c


def test_valid_contract():
    schema.validate("contract", contract())


@pytest.mark.parametrize("mut", [
    lambda c: c.update(extra=1),
    lambda c: c.pop("nonce"),
    lambda c: c.update(v=2),
    lambda c: c.update(action="shell.exec"),
    lambda c: c["params"].update(url="https://evil"),
    lambda c: c["params"].update(commit="B" * 40),
    lambda c: c["params"].update(scheme="Demo\n"),
    lambda c: c.update(issued_at=True),
    lambda c: c.update(issued_at=-1),
    lambda c: c.update(approval_ref="hermes:x"),
    lambda c: c.update(action="system.info"),          # params then must be {}
])
def test_invalid_contracts(mut):
    c = contract()
    mut(c)
    with pytest.raises(SchemaError):
        schema.validate("contract", c)


def test_dollar_anchor_does_not_accept_trailing_newline():
    with pytest.raises(SchemaError):
        schema.validate("job-cancel", {"job_id": UUID + "\n"})


def test_error_accepts_helper_busy():
    schema.validate("error", {"code": "helper_busy", "message": "busy", "detail": {"reason": "queue_full"},
                              "retry_after_s": 30})
    with pytest.raises(SchemaError):
        schema.validate("error", {"code": "nope", "message": "x"})


def test_oneof_result_shapes():
    schema.validate("job-result", {"job_id": UUID, "state": "succeeded", "error": None, "result": {
        "os_version": "26.2", "os_build": "25C56", "arch": "arm64", "cpu": "Apple M4", "memory_gb": 16,
        "xcode": None, "disk_free_gb": 100}})
    with pytest.raises(SchemaError):
        schema.validate("job-result", {"job_id": UUID, "state": "succeeded", "error": None,
                                       "result": {"hostname": "x"}})
