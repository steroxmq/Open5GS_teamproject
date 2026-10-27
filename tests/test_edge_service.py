"""HTTP acceptance and data-path evidence checks; no Docker is needed here."""
import importlib.util
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


app = load("lab_edge_server", ROOT / "docker/edge/server.py")
check = load("lab_edge_check", ROOT / "scripts/check-edge.py")


class EdgeHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.EdgeServer(("127.0.0.1", 0), "127.0.0.1")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, payload=None, raw=None, headers=None, port=None):
        if raw is None and payload is not None:
            raw = json.dumps(payload).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port or self.server.server_port, timeout=3)
        try:
            conn.request(method, path, raw, headers or {"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    @staticmethod
    def reading():
        return {"sensor_id": "ue-sensor-1", "request_id": "unique-request-1", "temperature_c": 23.5}

    def test_health(self):
        self.assertEqual(self.request("GET", "/health"),
                         (200, {"status": "ok", "service": "ue-sensor-edge"}))

    def test_sensor_reading_is_processed_and_correlated(self):
        status, body = self.request("POST", "/readings", self.reading())
        self.assertEqual(status, 201)
        self.assertEqual(body["temperature_c"], 23.5)
        self.assertEqual(body["temperature_k"], 296.65)
        self.assertEqual(body["request_id"], "unique-request-1")
        self.assertEqual(body["sensor_id"], "ue-sensor-1")
        self.assertEqual(body["observed_source_ip"], "127.0.0.1")
        self.assertIn("received_at", body)

    def test_other_source_is_not_accepted_as_upf(self):
        server = app.EdgeServer(("127.0.0.1", 0), "172.18.0.7")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = self.request("POST", "/readings", self.reading(), port=server.server_port)
            self.assertEqual(status, 403)
            self.assertEqual(body["error"], "expected_upf_source")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_invalid_sensor_values_do_not_crash_server(self):
        for value in [True, None, "23.5", float("nan"), float("inf"), -273.16, 1001, 10 ** 1000]:
            with self.subTest(value=type(value).__name__):
                data = self.reading()
                data["temperature_c"] = value
                status, body = self.request("POST", "/readings", data)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "invalid_temperature")
        self.assertEqual(self.request("GET", "/health")[0], 200)

    def test_absolute_zero_is_valid(self):
        data = self.reading()
        data["temperature_c"] = -273.15
        status, body = self.request("POST", "/readings", data)
        self.assertEqual((status, body["temperature_k"]), (201, 0))

    def test_invalid_fields_and_identifiers(self):
        cases = [[], {}, dict(self.reading(), extra=1),
                 dict(self.reading(), sensor_id=""), dict(self.reading(), request_id="bad/id")]
        for data in cases:
            with self.subTest(data=data):
                self.assertEqual(self.request("POST", "/readings", data)[0], 400)

    def test_bad_json(self):
        self.assertEqual(self.request("POST", "/readings", raw=b"{broken")[0], 400)

    def test_body_limit(self):
        self.assertEqual(self.request("POST", "/readings", raw=b"x" * 4097)[0], 413)

    def test_content_type(self):
        self.assertEqual(self.request("POST", "/readings", self.reading(),
                                      headers={"Content-Type": "text/plain"})[0], 415)

    def test_unknown_endpoint(self):
        self.assertEqual(self.request("GET", "/missing")[0], 404)


class EvidenceValidationTests(unittest.TestCase):
    def setUp(self):
        self.reading = {"sensor_id": "ue-sensor-1", "request_id": "fresh-request", "temperature_c": 23.5}
        self.good = {"http_status": 201, "client_source_ip": "10.45.0.3", "elapsed_ms": 3.4,
                     "response": dict(self.reading, status="accepted", temperature_k=296.65,
                                      observed_source_ip="172.18.0.7")}

    def test_valid_receipt(self):
        check.validate_response(self.good, self.reading, "10.45.0.3")

    def test_replayed_receipt_is_rejected(self):
        self.good["response"]["request_id"] = "old-request"
        with self.assertRaises(check.LabError):
            check.validate_response(self.good, self.reading, "10.45.0.3")

    def test_direct_docker_path_is_rejected(self):
        self.good["response"]["observed_source_ip"] = "172.18.0.22"
        with self.assertRaises(check.LabError):
            check.validate_response(self.good, self.reading, "10.45.0.3")

    def test_wrong_client_source_is_rejected(self):
        self.good["client_source_ip"] = "172.18.0.22"
        with self.assertRaises(check.LabError):
            check.validate_response(self.good, self.reading, "10.45.0.3")

    def test_malformed_receipts_are_rejected_and_reported(self):
        for value in [[], {}, {"response": []}, {"response": None}]:
            with self.subTest(value=value):
                with self.assertRaises(check.LabError):
                    check.validate_response(value, self.reading, "10.45.0.3")
                with tempfile.TemporaryDirectory() as directory:
                    check.write_report(Path(directory), {"status": "failed", "http": value, "error": "invalid receipt"})
                    self.assertTrue((Path(directory) / "summary.json").is_file())


if __name__ == "__main__":
    unittest.main()
