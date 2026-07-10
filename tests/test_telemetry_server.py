"""E2E-тест приёмника telemetry_server.py: реальный HTTP на эфемерном порту через urllib."""
import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


srv_mod = _load("telemetry_server")

TOKEN = "test-token-abc123"


def _valid_record(uuid="a1b2c3d4e5f6"):
    return {
        "schema_version": "1",
        "plugin_version": "1.2.3",
        "machine_id": "0123456789abcdef",
        "plan_id": "fedcba9876543210",
        "run_uuid": uuid,
        "stop_reason": "complete",
        "run_wall_s": 100,
        "sessions_total": 2,
        "phases_total": 2,
        "status_counts": {"done": 2},
        "has_codebase_map": True,
        "phases": [{"ordinal": 10, "outcome": "done", "files_changed": 1}],
    }


class TestServer(unittest.TestCase):
    def setUp(self):
        self.store = tempfile.mkdtemp()
        self.srv = srv_mod.make_server("127.0.0.1", 0, TOKEN, self.store)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _req(self, path, method="POST", body=None, token=TOKEN, raw=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(url, data=data, method=method)
        if token is not None:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_health_ok(self):
        self.assertEqual(self._req("/health", method="GET")[0], 200)

    def test_health_bad_token(self):
        self.assertEqual(self._req("/health", method="GET", token="wrong")[0], 401)

    def test_ingest_writes_file(self):
        status, payload = self._req("/ingest", body=_valid_record())
        self.assertEqual(status, 201)
        files = os.listdir(self.store)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith("-a1b2c3d4e5f6.json"))
        with open(os.path.join(self.store, files[0])) as f:
            self.assertEqual(json.load(f)["stop_reason"], "complete")

    def test_ingest_overwrite_last(self):
        # Снимки одного прогона (тот же run_uuid) склеиваются: последний выигрывает, файл один.
        first = _valid_record()
        first["stop_reason"] = "hang"
        self.assertEqual(self._req("/ingest", body=first)[0], 201)
        later = _valid_record()  # тот же uuid, но прогон уже complete и с большим числом фаз
        later["phases"] = [
            {"ordinal": 10, "outcome": "done", "files_changed": 1},
            {"ordinal": 20, "outcome": "done", "files_changed": 2},
        ]
        status, payload = self._req("/ingest", body=later)
        self.assertEqual(status, 200)
        self.assertTrue(payload["updated"])
        files = os.listdir(self.store)
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.store, files[0])) as f:
            data = json.load(f)
        self.assertEqual(data["stop_reason"], "complete")  # финальный снимок победил
        self.assertEqual(len(data["phases"]), 2)

    def test_overwrite_keys_on_exact_ids_not_just_uuid(self):
        # Склейка — по точному триплету (machine+plan+uuid), не по одному uuid: запись с тем же
        # run_uuid, но иным machine_id (другой продюсер) НЕ затирает чужую — пишется отдельным файлом.
        a = _valid_record()
        b = _valid_record()
        b["machine_id"] = "ffffffffffffffff"  # иной продюсер, тот же uuid
        self.assertEqual(self._req("/ingest", body=a)[0], 201)
        self.assertEqual(self._req("/ingest", body=b)[0], 201)  # не 200/updated — новый файл
        self.assertEqual(len(os.listdir(self.store)), 2)

    def test_ingest_bad_token(self):
        self.assertEqual(self._req("/ingest", body=_valid_record(), token="nope")[0], 401)
        self.assertEqual(os.listdir(self.store), [])

    def test_ingest_bad_json(self):
        self.assertEqual(self._req("/ingest", raw=b"{not json", token=TOKEN)[0], 400)

    def test_ingest_bad_envelope(self):
        rec = _valid_record()
        del rec["run_uuid"]
        self.assertEqual(self._req("/ingest", body=rec)[0], 400)

    def test_ingest_scrub_rejects_leak(self):
        rec = _valid_record()
        rec["phases"][0]["path"] = "src/secret.py"  # путь — скраб уронит
        self.assertEqual(self._req("/ingest", body=rec)[0], 400)
        self.assertEqual(os.listdir(self.store), [])

    def test_ingest_too_large(self):
        rec = _valid_record()
        rec["junk"] = "a" * (300 * 1024)  # > MAX_BODY
        self.assertEqual(self._req("/ingest", body=rec)[0], 413)


if __name__ == "__main__":
    unittest.main()
