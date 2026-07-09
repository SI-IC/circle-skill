"""E2E клиента send/activate против живого telemetry_server на эфемерном порту."""
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tele = _load("circle_telemetry")
srv_mod = _load("telemetry_server")

TOKEN = "client-test-token-xyz"


def _record(uuid):
    return {
        "schema_version": "1", "plugin_version": "1.0.0",
        "machine_id": "0123456789abcdef", "plan_id": "fedcba9876543210",
        "run_uuid": uuid, "stop_reason": "complete", "run_wall_s": 5,
        "sessions_total": 1, "phases_total": 1, "status_counts": {"done": 1},
        "has_codebase_map": True, "phases": [],
    }


class TestClient(unittest.TestCase):
    def setUp(self):
        self.store = tempfile.mkdtemp()
        self.work = tempfile.mkdtemp()
        self.outbox = os.path.join(self.work, "run-stats", "outbox")
        os.makedirs(self.outbox)
        self.srv = srv_mod.make_server("127.0.0.1", 0, TOKEN, self.store)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _stage(self, uuid):
        with open(os.path.join(self.outbox, uuid + ".json"), "w") as f:
            json.dump(_record(uuid), f)

    def test_send_delivers_and_clears_outbox(self):
        self._stage("aaaaaaaaaaaa")
        self._stage("bbbbbbbbbbbb")
        r = tele.send_outbox(self.work, self.url, TOKEN)
        self.assertEqual(r["sent"], 2)
        self.assertEqual(r["failed"], 0)
        self.assertEqual(r["reason"], "ok")
        self.assertEqual(os.listdir(self.outbox), [])            # outbox очищен
        self.assertEqual(len(os.listdir(self.store)), 2)         # сервер сохранил
        with open(os.path.join(self.work, "run-stats", "sent.log")) as f:
            self.assertEqual(len(f.read().strip().splitlines()), 2)

    def test_send_empty_outbox(self):
        r = tele.send_outbox(self.work, self.url, TOKEN)
        self.assertEqual(r, {"sent": 0, "failed": 0, "reason": "нет-данных"})

    def test_send_bad_token_keeps_outbox(self):
        self._stage("cccccccccccc")
        r = tele.send_outbox(self.work, self.url, "wrong-token")
        self.assertEqual(r["sent"], 0)
        self.assertEqual(r["failed"], 1)
        self.assertEqual(r["reason"], "401")
        self.assertEqual(os.listdir(self.outbox), ["cccccccccccc.json"])  # не потеряли

    def test_send_no_url_not_configured(self):
        self._stage("dddddddddddd")
        r = tele.send_outbox(self.work, "", TOKEN)
        self.assertEqual(r["reason"], "не-настроено")
        self.assertEqual(os.listdir(self.outbox), ["dddddddddddd.json"])

    def test_send_dedup_second_run(self):
        self._stage("eeeeeeeeeeee")
        tele.send_outbox(self.work, self.url, TOKEN)
        self._stage("eeeeeeeeeeee")  # тот же uuid снова в outbox
        r = tele.send_outbox(self.work, self.url, TOKEN)
        self.assertEqual(r["sent"], 1)                    # 200 duplicate тоже «доставлено»
        self.assertEqual(len(os.listdir(self.store)), 1)  # дубля на сервере нет

    def test_activate_ping_ok(self):
        ok, reason = tele.ping(self.url, TOKEN)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_activate_ping_bad_token(self):
        ok, reason = tele.ping(self.url, "nope")
        self.assertFalse(ok)
        self.assertEqual(reason, "401")

    def test_activate_ping_no_server(self):
        ok, reason = tele.ping("http://127.0.0.1:1", TOKEN)
        self.assertFalse(ok)
        self.assertEqual(reason, "нет-связи")

    def test_send_rejects_plain_http_external(self):
        # голый http на внешний хост — токен НЕ уходит в открытом виде, outbox сохранён
        self._stage("ffffffffffff")
        r = tele.send_outbox(self.work, "http://example.com:3000", TOKEN)
        self.assertEqual(r["reason"], "небезопасный-url")
        self.assertEqual(r["sent"], 0)
        self.assertEqual(os.listdir(self.outbox), ["ffffffffffff.json"])

    def test_activate_rejects_plain_http_external(self):
        ok, reason = tele.ping("http://example.com", TOKEN)
        self.assertFalse(ok)
        self.assertEqual(reason, "небезопасный-url")

    def test_https_url_allowed(self):
        self.assertTrue(tele._url_ok("https://x.example.com/path"))
        self.assertTrue(tele._url_ok("http://127.0.0.1:3000"))
        self.assertTrue(tele._url_ok("http://localhost:8080"))
        self.assertFalse(tele._url_ok("http://10.0.0.5:3000"))
        self.assertFalse(tele._url_ok("ftp://x"))

    def test_send_poison_record_moved_to_rejected(self):
        # запись, которую приёмник отвергнет навсегда (битый envelope → 400), уходит из outbox
        # в rejected/, а не ретраится вечно
        with open(os.path.join(self.outbox, "bad.json"), "w") as f:
            f.write('{"schema_version":"1"}')  # нет run_uuid → 400 bad_envelope
        r = tele.send_outbox(self.work, self.url, TOKEN)
        self.assertEqual(r["sent"], 0)
        self.assertEqual(r["failed"], 1)
        self.assertEqual(r["reason"], "отклонено-приёмником")
        self.assertEqual(os.listdir(self.outbox), [])  # из outbox убрано
        rej = os.path.join(self.work, "run-stats", "rejected")
        self.assertEqual(os.listdir(rej), ["bad.json"])


if __name__ == "__main__":
    unittest.main()
