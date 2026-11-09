"""Minimal fake OpenAI-compatible /v1/chat/completions server for probes.

Script: FAKE_SCRIPT env = JSON list of steps; each step is either {"text": "..."} or
{"tool": "name", "args": {...}}. Steps are consumed per request; after the list ends it
answers "done". Every request body is appended to FAKE_LOG (jsonl).
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("FAKE_LOG", "fake_llm.jsonl")
SCRIPT = json.loads(os.environ.get("FAKE_SCRIPT", "[]"))
_lock = threading.Lock()
_i = [0]


def _next():
    with _lock:
        i = _i[0]
        _i[0] += 1
    return SCRIPT[i] if i < len(SCRIPT) else {"text": "done"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "fake-model", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        with _lock, open(LOG, "a") as fh:
            fh.write(json.dumps({"path": self.path, "body": req}) + "\n")
        step = _next() if (self.path.endswith("/chat/completions") and req.get("tools")) else {"text": "aux"}
        stream = bool(req.get("stream"))
        cid = f"chatcmpl-{int(time.time()*1000)}"
        if "tool" in step:
            tc = {"index": 0, "id": f"call_{cid}", "type": "function",
                  "function": {"name": step["tool"], "arguments": json.dumps(step.get("args", {}))}}
            msg = {"role": "assistant", "content": None, "tool_calls": [tc]}
            finish = "tool_calls"
        else:
            msg = {"role": "assistant", "content": step["text"]}
            finish = "stop"
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if not stream:
            body = json.dumps({"id": cid, "object": "chat.completion", "created": int(time.time()),
                               "model": req.get("model", "fake-model"),
                               "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                               "usage": usage}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def ev(delta, fin=None, use=None):
            d = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                 "model": req.get("model", "fake-model"),
                 "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
            if use:
                d["usage"] = use
            self.wfile.write(b"data: " + json.dumps(d).encode() + b"\n\n")

        if "tool" in step:
            ev({"role": "assistant", "tool_calls": [tc]})
        else:
            ev({"role": "assistant", "content": step["text"]})
        ev({}, finish, usage)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(srv.server_address[1], flush=True)
    srv.serve_forever()
