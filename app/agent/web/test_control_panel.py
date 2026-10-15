"""Control-panel supervisor + API: the security-sensitive seams.

Covers the admin-token gate, the self-signed cert, parameter validation (no arbitrary argv reaches
a child), that the OpenRouter key is passed to dev_server in the ENVIRONMENT and never on the
command line, key masking/0600 files, RAG discovery and spend aggregation. Child processes are
faked — nothing real is launched here.

Run:  python3 -m unittest agent.web.test_control_panel
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from . import control_panel as cp


class _FakeProc:
    def __init__(self, argv, **kw):
        self.argv = argv
        self.kw = kw
        self.pid = 4321
        self._poll = None

    def poll(self):
        return self._poll

    def terminate(self):
        self._poll = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._poll = -9


class ControlPanelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cp-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Redirect every state path into the temp dir.
        self._patches = [
            patch.object(cp, "TOKEN_FILE", self.tmp / "token.txt"),
            patch.object(cp, "CERT_FILE", self.tmp / "cert.pem"),
            patch.object(cp, "CERT_KEY_FILE", self.tmp / "key.pem"),
            patch.object(cp, "OPENROUTER_KEY_FILE", self.tmp / "or.txt"),
            patch.object(cp, "PANEL_CONFIG_FILE", self.tmp / "panel.json"),
            patch.object(config, "WEB_UI_API_KEY_FILE", self.tmp / "web.txt"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    # ── token ──
    def test_token_generated_once_and_0600(self):
        t1 = cp.ensure_token()
        t2 = cp.ensure_token()
        self.assertEqual(t1, t2)
        self.assertGreater(len(t1), 20)
        self.assertEqual(oct(os.stat(cp.TOKEN_FILE).st_mode & 0o777), "0o600")

    def test_api_requires_token(self):
        from fastapi.testclient import TestClient
        with patch.object(cp, "_TOKEN", "secret-token"):
            c = TestClient(cp.app)
            self.assertEqual(c.get("/api/status").status_code, 401)
            self.assertEqual(
                c.get("/api/status", headers={"X-Admin-Token": "wrong"}).status_code, 401)
            self.assertEqual(
                c.get("/api/status", headers={"X-Admin-Token": "secret-token"}).status_code, 200)

    # ── TLS cert ──
    def test_cert_generated_with_sans_and_private_key_0600(self):
        cert, keyf = cp.ensure_cert()
        self.assertTrue(cert.exists() and keyf.exists())
        self.assertEqual(oct(os.stat(keyf).st_mode & 0o777), "0o600")
        from cryptography import x509
        loaded = x509.load_pem_x509_certificate(cert.read_bytes())
        san = loaded.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        ips = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
        self.assertIn("127.0.0.1", ips)

    # ── validation: no arbitrary argv ──
    def test_validate_rejects_bad_values(self):
        s = cp.Supervisor()
        for bad in [
            {"provider": "evil", "model": "m"},
            {"provider": "openrouter", "model": "bad model; rm -rf /"},
            {"provider": "openrouter", "model": "x/y", "host": "8.8.8.8"},
            {"provider": "openrouter", "model": "x/y", "port": 80},
            {"provider": "openrouter", "model": "x/y", "vector_index": "/no/such/file.npy"},
        ]:
            with self.assertRaises(ValueError):
                s._validate_dev(bad)

    def test_validate_accepts_good(self):
        s = cp.Supervisor()
        clean = s._validate_dev({"provider": "openrouter", "model": "deepseek/deepseek-v4-pro",
                                 "host": "0.0.0.0", "port": 7777})
        self.assertEqual(clean["model"], "deepseek/deepseek-v4-pro")

    # ── the key goes in the ENV, never argv ──
    def test_openrouter_key_in_env_not_argv(self):
        cp._write_private(cp.OPENROUTER_KEY_FILE, "sk-or-SECRET123\n")
        s = cp.Supervisor()
        with patch.object(cp.subprocess, "Popen", _FakeProc):
            s.start_dev({"provider": "openrouter", "model": "deepseek/deepseek-v4-pro",
                         "host": "127.0.0.1", "port": 7777})
            proc = s.dev
        self.assertNotIn("sk-or-SECRET123", " ".join(proc.argv), "key must not be on the command line")
        self.assertEqual(proc.kw["env"]["OPENROUTER_API_KEY"], "sk-or-SECRET123")
        self.assertIn("--model", proc.argv)
        self.assertIn("deepseek/deepseek-v4-pro", proc.argv)

    def test_double_start_refused(self):
        s = cp.Supervisor()
        with patch.object(cp.subprocess, "Popen", _FakeProc):
            s.start_dev({"provider": "openrouter", "model": "a/b"})
            with self.assertRaises(RuntimeError):
                s.start_dev({"provider": "openrouter", "model": "a/b"})

    # ── keys: masked read, write-only, 0600 ──
    def test_openrouter_key_masked_and_private(self):
        from fastapi.testclient import TestClient
        with patch.object(cp, "_TOKEN", "t"):
            c = TestClient(cp.app)
            h = {"X-Admin-Token": "t"}
            r = c.post("/api/keys/openrouter", json={"key": "sk-or-abcd1234WXYZ"}, headers=h)
            self.assertTrue(r.json()["ok"])
            self.assertEqual(oct(os.stat(cp.OPENROUTER_KEY_FILE).st_mode & 0o777), "0o600")
            got = c.get("/api/keys/openrouter", headers=h).json()
            self.assertTrue(got["set"])
            self.assertEqual(got["last4"], "WXYZ")
            self.assertNotIn("abcd1234", str(got))  # full key never returned

    def test_rotate_web_key_sets_file_and_returns_once(self):
        from fastapi.testclient import TestClient
        with patch.object(cp, "_TOKEN", "t"):
            c = TestClient(cp.app)
            r = c.post("/api/keys/webui/rotate", headers={"X-Admin-Token": "t"}).json()
            self.assertTrue(r["ok"])
            self.assertGreater(len(r["key"]), 20)
            self.assertEqual(config.WEB_UI_API_KEY_FILE.read_text().strip(), r["key"])

    # ── discovery + spend ──
    def test_rag_discovery_finds_pair(self):
        root = self.tmp / "proj" / "knowledge_rag" / "index"
        root.mkdir(parents=True)
        import numpy as np
        np.save(root / "vectors.npy", np.zeros((3, 4), dtype="float32"))
        (root / "meta.jsonl").write_text('{"id":"a"}\n')
        with patch.object(cp, "_DEFAULT_RAG_ROOTS", [self.tmp]):
            found = cp.discover_rag_indexes()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["chunks"], 3)

    def test_spend_aggregation(self):
        eng = self.tmp / "engagements" / "e1"
        eng.mkdir(parents=True)
        conn = sqlite3.connect(eng / "spend.db")
        conn.execute("CREATE TABLE reservations (reservation_id TEXT, amount REAL, actual REAL, "
                     "status TEXT, worker TEXT, created_at REAL, settled_at REAL)")
        conn.execute("INSERT INTO reservations VALUES ('r1', 0.5, 0.42, 'settled', null, 0, 0)")
        conn.execute("INSERT INTO reservations VALUES ('r2', 0.5, null, 'reserved', null, 0, 0)")
        conn.commit(); conn.close()
        with patch.object(config, "ENGAGEMENTS_ROOT", self.tmp / "engagements"), \
             patch.object(config, "ENGAGEMENT_DIR", self.tmp / "none"):
            res = cp._engagement_spend_total()
        self.assertAlmostEqual(res["total"], 0.42)  # only settled actuals counted
        self.assertEqual(res["engagements"][0]["id"], "e1")

    def test_usage_no_key(self):
        res = cp._openrouter_usage()
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
