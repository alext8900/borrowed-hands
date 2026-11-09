"""Generate protocol/schemas/*.json for Borrowed Hands protocol v1. Usage: python3 gen_schemas.py <out_dir>"""
import json
import sys
from pathlib import Path

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
BASE = "https://borrowed-hands.dev/protocol/v1/"
UUID4 = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}
B64U = lambda n=None: ({"type": "string", "pattern": "^[A-Za-z0-9_-]{%d}$" % n} if n else
                       {"type": "string", "pattern": "^[A-Za-z0-9_-]+$"})
SIG = B64U(86)          # 64-byte Ed25519 signature
PUB = B64U(43)          # 32-byte Ed25519 public key
NONCE32 = B64U(43)
NONCE16 = B64U(22)
KEYID = {"type": "string", "pattern": "^[0-9a-f]{16}$"}
HEX64 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
SHA40 = {"type": "string", "pattern": "^[0-9a-f]{40}$"}
MS = {"type": "integer", "minimum": 0, "maximum": 9007199254740991}
NAME = {"type": "string", "pattern": "^[A-Za-z0-9_][A-Za-z0-9 ._+-]{0,63}$"}
WSID = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$"}
DEST = {"type": "string", "minLength": 1, "maxLength": 200}
ACTION = {"enum": ["xcode.build", "screen.capture", "system.info"]}
STATE = {"enum": ["received", "queued", "materializing", "building", "succeeded", "failed", "cancelled", "expired"]}
CODES = ["denied", "needs_grant", "helper_unavailable", "helper_busy", "invalid_arguments", "expired", "replay", "timeout",
         "cancelled", "needs_resolution", "internal_error"]


def obj(props, required=None, extra=False):
    return {"type": "object", "properties": props,
            "required": sorted(props) if required is None else required,
            "additionalProperties": extra}


def write(name, schema, title):
    doc = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": BASE + name, "title": title, **schema}
    (OUT / name).write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n")


write("envelope.json", obj({"v": {"const": 1}, "p": B64U(), "s": SIG}, ["v", "p"]),
      "Frame envelope. 's' is required for every frame after auth_ok/pair_ok (PROTOCOL 2.1, 2.2).")

PAYLOAD_PROPS = {
    "type": {"type": "string", "pattern": "^[a-z_]+(\\.[a-z_]+)?$"},
    "id": UUID4, "re": UUID4, "sid": UUID4,
    "seq": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
    "ts": MS, "exp": MS, "body": {"type": "object"}}
write("payload.json", obj(PAYLOAD_PROPS, ["type", "id", "sid", "seq", "ts", "exp", "body"]),
      "Decoded payload of a signed envelope, every frame after auth_ok/pair_ok (PROTOCOL 2.3). "
      "Also enforce exp - ts <= 120000 and now <= exp + 60000 (not expressible here).")
write("payload-handshake.json",
      obj({k: v for k, v in PAYLOAD_PROPS.items() if k not in ("sid", "seq", "exp")}, ["type", "id", "ts", "body"]),
      "Decoded payload of an unsigned handshake envelope: hello, auth, auth_ok, pair, pair_ok, error (PROTOCOL 2.1).")

write("error.json", obj({
    "code": {"enum": CODES}, "message": {"type": "string", "maxLength": 1000},
    "detail": {"type": "object"}, "retry_after_s": {"type": "integer", "minimum": 0, "maximum": 3600}},
    ["code", "message"]), "error body (PROTOCOL 2.4)")

write("hello.json", obj({"v": {"const": 1}, "server_nonce": NONCE32, "server_key_id": KEYID,
                         "server_time_ms": MS, "max_frame": {"const": 16777216}}), "hello body")
write("auth.json", obj({"helper_id": UUID4, "client_nonce": NONCE32, "device_sig": SIG}), "auth body")
write("auth-ok.json", obj({"sid": UUID4, "server_sig": SIG, "server_time_ms": MS}), "auth_ok body")
write("pair.json", obj({
    "code": NONCE16, "device_pubkey": PUB, "device_name": NAME, "client_nonce": NONCE32,
    "helper_version": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$"},
    "os": {"const": "macos"}, "os_version": {"type": "string", "pattern": "^[0-9]+(\\.[0-9]+){0,3}$"},
    "arch": {"enum": ["arm64", "x86_64"]}, "pair_sig": SIG}), "pair body (PROTOCOL 3.3)")
write("pair-ok.json", obj({"helper_id": UUID4, "sid": UUID4, "server_sig": SIG}), "pair_ok body")

workspace = obj({"workspace_id": WSID, "repo_fingerprint": HEX64,
                 "schemes": {"type": "array", "items": NAME, "maxItems": 32},
                 "configurations": {"type": "array", "items": NAME, "maxItems": 8},
                 "destinations": {"type": "array", "items": DEST, "maxItems": 8},
                 "expires_at": {"type": ["integer", "null"], "minimum": 0}})
cap = {"oneOf": [
    obj({"name": {"const": "system.info"}, "v": {"const": 1}}),
    obj({"name": {"const": "screen.capture"}, "v": {"const": 1}, "displays": {"type": "integer", "minimum": 1, "maximum": 16}}),
    obj({"name": {"const": "xcode.build"}, "v": {"const": 1},
         "xcode": {"type": "string", "pattern": "^[0-9]+(\\.[0-9]+){0,2}$"},
         "xcode_build": {"type": "string", "pattern": "^[0-9A-Za-z]{1,16}$"},
         "workspaces": {"type": "array", "items": workspace, "maxItems": 32}})]}
write("caps.json", obj({
    "helper_version": {"type": "string"}, "name": NAME, "os": {"const": "macos"},
    "os_version": {"type": "string", "pattern": "^[0-9]+(\\.[0-9]+){0,3}$"},
    "arch": {"enum": ["arm64", "x86_64"]}, "busy": {"type": "boolean"},
    "queue_depth": {"type": "integer", "minimum": 0, "maximum": 100},
    "capabilities": {"type": "array", "items": cap, "maxItems": 8}}), "caps body (PROTOCOL 6.1)")

write("params-xcode-build.json", obj({
    "workspace_id": WSID, "repo_fingerprint": HEX64, "commit": SHA40,
    "scheme": NAME, "configuration": NAME, "destination": DEST}), "contract params for xcode.build")
write("params-screen-capture.json", obj({
    "display": {"type": "integer", "minimum": 1, "maximum": 16},
    "max_px": {"type": "integer", "minimum": 256, "maximum": 2000}}), "contract params for screen.capture")
write("params-system-info.json", obj({}), "contract params for system.info")

contract = obj({
    "v": {"const": 1}, "job_id": UUID4, "nonce": NONCE16, "helper_id": UUID4, "server_key_id": KEYID,
    "action": ACTION, "params": {"type": "object"}, "issued_at": MS, "expires_at": MS,
    "approval_ref": {"type": "string", "pattern": "^(none|hermes:[0-9a-f-]{36})$"}})
contract["allOf"] = [
    {"if": {"properties": {"action": {"const": a}}}, "then": {"properties": {"params": {"$ref": f}}}}
    for a, f in [("xcode.build", "params-xcode-build.json"), ("screen.capture", "params-screen-capture.json"),
                 ("system.info", "params-system-info.json")]]
write("contract.json", contract,
      "Signed action contract (PROTOCOL 4.1). Verify the signature over the raw bytes BEFORE parsing. "
      "Also enforce expires_at - issued_at <= 600000 (not expressible here).")

write("job-submit.json", obj({"contract": B64U(), "sig": SIG}), "job.submit body")
write("job-state.json", obj({"job_id": UUID4, "state": STATE, "at": MS,
                             "position": {"type": "integer", "minimum": 0}}, ["job_id", "state", "at"]),
      "job.accepted / job.state body")
write("job-cancel.json", obj({"job_id": UUID4}), "job.cancel body")
write("job-ack.json", obj({"job_ids": {"type": "array", "items": UUID4, "maxItems": 500}}), "job.ack body")

issue = obj({"file": {"type": "string", "maxLength": 500, "pattern": "^(<sdk>|<outside>|[^/\\u0000-\\u001f][^\\u0000-\\u001f]*)$"}, "line": {"type": "integer", "minimum": 0},
             "column": {"type": "integer", "minimum": 0}, "message": {"type": "string", "maxLength": 500},
             "type": {"type": "string", "maxLength": 100}}, ["message", "type"])
build_result = obj({
    "status": {"enum": ["succeeded", "failed"]}, "duration_ms": MS, "xcode": {"type": "string"},
    "destination": DEST, "error_count": {"type": "integer", "minimum": 0},
    "warning_count": {"type": "integer", "minimum": 0},
    "errors": {"type": "array", "items": issue, "maxItems": 50},
    "warnings": {"type": "array", "items": issue, "maxItems": 50},
    "log_tail": {"type": "string", "maxLength": 4000}, "truncated": {"type": "boolean"}})
capture_result = obj({"mime": {"const": "image/png"}, "width": {"type": "integer", "minimum": 1, "maximum": 2000},
                      "height": {"type": "integer", "minimum": 1, "maximum": 2000},
                      "display": {"type": "integer", "minimum": 1}, "png_b64": {"type": "string", "maxLength": 12582912}})
info_result = obj({"os_version": {"type": "string", "maxLength": 32}, "os_build": {"type": "string", "maxLength": 32},
                   "arch": {"enum": ["arm64", "x86_64"]}, "cpu": {"type": "string", "maxLength": 100},
                   "memory_gb": {"type": "integer", "minimum": 0},
                   "xcode": {"oneOf": [{"type": "null"}, obj({"version": {"type": "string", "maxLength": 32},
                                                               "build": {"type": "string", "maxLength": 32}})]},
                   "disk_free_gb": {"type": "integer", "minimum": 0}})
write("job-result.json", obj({
    "job_id": UUID4, "state": {"enum": ["succeeded", "failed", "cancelled", "expired"]},
    "result": {"oneOf": [{"type": "null"}, build_result, capture_result, info_result]},
    "error": {"oneOf": [{"type": "null"}, {"$ref": "error.json"}]}}), "job.result body (PROTOCOL 4.4)")

write("jobs-sync.json", obj({
    "active": {"type": "array", "maxItems": 100,
               "items": obj({"job_id": UUID4, "state": STATE})},
    "results": {"type": "array", "maxItems": 100, "items": {"$ref": "job-result.json"}}}), "jobs.sync body (PROTOCOL 4.5)")
write("device-rotate.json", obj({"new_pubkey": PUB, "sig_new": SIG}), "device.rotate body")
write("server-rotate.json", obj({"new_pubkey": PUB, "new_key_id": KEYID, "sig_new": SIG}), "server.rotate body")
write("unpaired.json", obj({"reason": {"type": "string", "maxLength": 200}}), "unpaired body")
write("empty.json", obj({}), "body of ack and unpair")
print("wrote", len(list(OUT.glob("*.json"))), "schemas")
