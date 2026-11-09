"""Real `hermes gateway run` (Hermes v0.22.7 from HERMES_SRC) + fake LLM + the fake helper.

1. The listener starts inside the gateway (and only there), a helper pairs and authenticates, the agent's
   hands_system_info call (issued by the fake LLM through the tool_call bridge, run through the api_server
   platform so the turn executes in the gateway process) reaches the helper and the result reaches the model.
2. PL-7: SIGTERM the gateway mid-job (on_unload is not called on SIGTERM), restart it, and the job is
   reconciled from the helper's jobs.sync.

Skipped unless HERMES_SRC points at a Hermes checkout with a .venv.
"""
import asyncio
import json
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

import bhproto as bp
from fake_helper import INFO, FakeHelper, now_ms

ROOT = Path(__file__).resolve().parents[2]
HERMES_SRC = os.environ.get("HERMES_SRC")
pytestmark = pytest.mark.skipif(not HERMES_SRC or not Path(HERMES_SRC, ".venv/bin/hermes").exists(),
                                reason="needs HERMES_SRC with a .venv")
FAKE_LLM = Path(__file__).resolve().parent / "fake_llm.py"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_until(pred, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Env:
    def __init__(self, tmp):
        self.home = tmp / "hermes-home"
        (self.home / "plugins").mkdir(parents=True)
        shutil.copytree(ROOT / "borrowed-hands", self.home / "plugins" / "borrowed-hands",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.bh_port, self.api_port, self.llm_port = free_port(), free_port(), free_port()
        self.url = f"ws://127.0.0.1:{self.bh_port}/bh/v1/ws"
        import hermes_yaml as yaml
        (self.home / "config.yaml").write_text(yaml.safe_dump({
            "plugins": {"enabled": ["borrowed-hands"], "disabled": [], "isolation": "in_process",
                        "entries": {"borrowed-hands": {"settings": {
                            "listen": {"enabled": True, "host": "127.0.0.1", "port": self.bh_port},
                            "public_url": self.url, "owners": ["local:cli"]}}}},
            "model": {"provider": "custom", "base_url": f"http://127.0.0.1:{self.llm_port}/v1",
                      "default": "fake-model"},
            "tools": {"tool_search": {"enabled": "auto"}}}))
        self.api_key = secrets.token_urlsafe(24)  # throwaway, local only
        self.env = dict(os.environ, HERMES_HOME=str(self.home), API_SERVER_KEY=self.api_key,
                        API_SERVER_PORT=str(self.api_port), API_SERVER_HOST="127.0.0.1")
        self.env.pop("BH_LISTEN", None)
        self.hermes = str(Path(HERMES_SRC) / ".venv" / "bin" / "hermes")
        self.gw = None
        self.llm = None
        self.n = 0

    def start_llm(self, script):
        self.llm_log = self.home.parent / "fake_llm.jsonl"
        self.llm = subprocess.Popen([sys.executable, str(FAKE_LLM), str(self.llm_port)],
                                    env=dict(self.env, FAKE_LOG=str(self.llm_log), FAKE_SCRIPT=json.dumps(script)),
                                    stdout=subprocess.PIPE, text=True)
        self.llm.stdout.readline()

    def start_gateway(self):
        self.n += 1
        self.gw_out = self.home.parent / f"gateway{self.n}.out"
        self.gw = subprocess.Popen([self.hermes, "gateway", "run", "--quiet"], env=self.env,
                                   stdout=open(self.gw_out, "w"), stderr=subprocess.STDOUT)
        wait_until(lambda: port_open(self.bh_port), 90, "the Borrowed Hands listener")
        wait_until(lambda: port_open(self.api_port), 90, "the api_server")

    def sigterm(self):
        self.gw.send_signal(signal.SIGTERM)
        try:
            self.gw.wait(40)
        except subprocess.TimeoutExpired:
            self.gw.kill()
            self.gw.wait(10)
        wait_until(lambda: not port_open(self.bh_port), 20, "the listener to go away")

    def stop(self):
        if self.gw and self.gw.poll() is None:
            self.sigterm()
        if self.llm:
            self.llm.terminate()

    def data_dir(self):
        found = list((self.home / "plugin-data").glob("*/server_ed25519.key"))
        assert len(found) == 1, found
        return found[0].parent

    def pairing_string(self):
        """Seed a code the way /hands pair does (the command path itself is covered in test_wiring.py)."""
        d = self.data_dir()
        seed = bp.unb64u((d / "server_ed25519.key").read_text().strip())
        pub = bp.pub_raw(bp.priv_from_seed(seed))
        code = os.urandom(16)
        c = sqlite3.connect(d / "bh.sqlite3", timeout=10)
        c.execute("UPDATE pair_codes SET used_at='superseded' WHERE used_at IS NULL")
        c.execute("INSERT INTO pair_codes(code_hash, created_at, expires_at, issued_to) VALUES(?,?,?,?)",
                  (bp.code_hash(code), now_ms(), now_ms() + 600_000, "local:cli"))
        c.commit()
        c.close()
        return bp.pairing_string(self.url, code, pub)

    def job_row(self, job_id):
        c = sqlite3.connect(self.data_dir() / "bh.sqlite3", timeout=10)
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT state, stale, result_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        c.close()
        return r

    def chat(self, text, timeout=120):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.api_port}/v1/chat/completions", method="POST",
            data=json.dumps({"model": "hermes", "messages": [{"role": "user", "content": text}]}).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def tool_messages(self):
        out = []
        for line in self.llm_log.read_text().splitlines():
            for m in json.loads(line)["body"].get("messages") or []:
                if m.get("role") == "tool":
                    out.append(m.get("content"))
        return out


@pytest.fixture
def e2e(tmp_path):
    e = Env(tmp_path)
    yield e
    e.stop()


CALL_INFO = {"tool": "tool_call", "args": {"name": "hands_system_info", "arguments": {}}}


def test_gateway_listener_pair_and_system_info_through_the_agent(e2e):
    e2e.start_llm([CALL_INFO, {"text": "The Mac runs macOS 26.2."}])
    e2e.start_gateway()
    with urllib.request.urlopen(f"http://127.0.0.1:{e2e.bh_port}/bh/v1/healthz") as r:
        assert json.loads(r.read()) == {"ok": True, "v": 1}
    s = e2e.pairing_string()
    served = threading.Event()

    async def helper():
        h = FakeHelper.from_pairing(s, name="E2E Mac")
        assert (await h.pair())["type"] == "pair_ok"
        await h.startup()
        served.set()
        sub = await h.recv(timeout=90)
        assert sub["type"] == "job.submit"
        c = await h.answer_system_info(sub)
        await asyncio.sleep(0.5)
        await h.close()
        return c

    out = {}
    t = threading.Thread(target=lambda: out.__setitem__("c", asyncio.run(helper())))
    t.start()
    assert served.wait(30)
    reply = e2e.chat("what Mac do I have?")
    t.join(60)
    c = out["c"]
    assert c["action"] == "system.info" and c["approval_ref"] == "none"
    tools_seen = e2e.tool_messages()
    assert any('"os_version": "26.2"' in (m or "") and '"ok": true' in (m or "") for m in tools_seen), tools_seen
    assert "macOS 26.2" in json.dumps(reply)
    log = e2e.gw_out.read_text() + "".join(
        p.read_text(errors="replace") for p in (e2e.home / "logs").glob("*.log"))
    assert "borrowed_hands" in log and "listener up" in log
    assert s.split("#")[1].split(".")[0] not in log


def test_validate_and_doctor_processes_do_not_listen(e2e):
    port_was_free = not port_open(e2e.bh_port)
    r = subprocess.run([e2e.hermes, "plugins", "validate", str(ROOT / "borrowed-hands")], env=e2e.env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Validation passed" in r.stdout and "dangerous" not in r.stdout
    assert port_was_free and not port_open(e2e.bh_port)


def test_sigterm_mid_job_then_restart_reconciles(e2e):
    """PL-7 with the real gateway: the job is accepted (queued), the gateway is SIGTERMed while the tool waits,
    a new gateway marks it stale, and the helper's jobs.sync result finishes it exactly once."""
    e2e.start_llm([CALL_INFO, {"text": "interrupted"}])
    e2e.start_gateway()
    s = e2e.pairing_string()
    accepted = threading.Event()
    state = {}

    async def phase1():
        h = FakeHelper.from_pairing(s)
        await h.pair()
        await h.startup()
        state["h"] = h
        sub = await h.recv(timeout=90)
        c = h.verify_contract(sub["body"])
        await h.send("job.accepted", {"job_id": c["job_id"], "state": "queued", "at": now_ms(), "position": 0},
                     re=sub["id"])
        state["c"] = c
        accepted.set()
        try:
            await h.expect_close(timeout=60)
        except Exception:
            pass
        await h.close()

    t = threading.Thread(target=lambda: asyncio.run(phase1()))
    t.start()
    time.sleep(1)
    chat_t = threading.Thread(target=lambda: _swallow(e2e.chat, "info please", 60))
    chat_t.start()
    assert accepted.wait(60)
    wait_until(lambda: e2e.job_row(state["c"]["job_id"])["state"] == "queued", 10, "queued")
    e2e.sigterm()
    t.join(30)
    chat_t.join(70)
    e2e.start_gateway()
    jid = state["c"]["job_id"]
    wait_until(lambda: e2e.job_row(jid)["stale"] == 1, 10, "the job to be marked stale")
    h = state["h"]

    async def phase2():
        h2 = FakeHelper(e2e.url, h.server_pub, device_key=h.key, helper_id=h.helper_id)
        await h2.auth()
        result = {"job_id": jid, "state": "succeeded", "result": INFO, "error": None}
        ack = await h2.startup(results=[result])
        await h2.send("job.result", {**result, "result": {**INFO, "cpu": "second copy"}})
        ack2 = await h2.recv()
        await h2.close()
        return ack, ack2
    ack, ack2 = asyncio.run(phase2())
    assert ack["body"]["job_ids"] == [jid] and ack2["body"]["job_ids"] == [jid]
    row = e2e.job_row(jid)
    assert row["state"] == "succeeded" and row["stale"] == 0 and json.loads(row["result_json"]) == INFO


def _swallow(fn, *a):
    try:
        fn(*a)
    except Exception:
        pass


def _listeners(port):
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
    return [line for line in out.splitlines()[1:] if line.strip()]


def test_reload_plugins_keeps_exactly_one_listener_and_helpers_reconnect(e2e):
    """PL-2: reload-plugins calls on_unload (listener closes sockets with 1001) and register again."""
    e2e.start_llm([])
    e2e.start_gateway()
    s = e2e.pairing_string()

    async def first():
        h = FakeHelper.from_pairing(s)
        await h.pair()
        await h.startup()
        return h
    h = asyncio.run(_keep_open(first, e2e))
    assert h["code"] == 1001
    wait_until(lambda: port_open(e2e.bh_port), 30, "the listener after reload")
    assert len(_listeners(e2e.bh_port)) == 1
    helper = h["helper"]

    async def again():
        h2 = FakeHelper(e2e.url, helper.server_pub, device_key=helper.key, helper_id=helper.helper_id)
        await h2.auth()
        await h2.startup()
        await h2.close()
    asyncio.run(again())


async def _keep_open(first, e2e):
    from gateway.control_socket import reload_gateway_plugins
    h = await first()
    ans = await asyncio.to_thread(reload_gateway_plugins, e2e.home)
    assert ans and ans.get("reloaded"), ans
    code = await h.expect_close(timeout=30)
    await h.close()
    return {"code": code, "helper": h}
