#!/usr/bin/env python3
"""Small internal lab service: validate a UE sensor reading and convert C to K."""
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
import re
import threading

MAX_BODY = 4096
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class EdgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, upf_ip):
        self.upf_ip = str(ipaddress.IPv4Address(upf_ip))
        self.count = 0
        self.count_lock = threading.Lock()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "Open5GS-Lab-Edge/1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def send_json(self, status, payload):
        raw = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(raw)

    def do_GET(self):
        if self.path != "/health":
            self.send_json(404, {"error": "not_found"})
            return
        self.send_json(200, {"status": "ok", "service": "ue-sensor-edge"})

    def do_POST(self):
        if self.path != "/readings":
            self.send_json(404, {"error": "not_found"})
            return
        # The normal UPF MASQUERADE rule translates a UE source to this address.
        # Direct POST requests from the UE's Docker eth0 address are rejected.
        peer = self.client_address[0]
        if peer != self.server.upf_ip:
            self.send_json(403, {"error": "expected_upf_source", "observed_source": peer})
            return
        if self.headers.get("Transfer-Encoding"):
            self.send_json(400, {"error": "unsupported_transfer_encoding"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self.send_json(411, {"error": "content_length_required"})
            return
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,6}", lengths[0]):
            self.send_json(400, {"error": "invalid_content_length"})
            return
        size = int(lengths[0])
        if size > MAX_BODY:
            self.send_json(413, {"error": "body_too_large"})
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json(415, {"error": "application_json_required"})
            return
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                self.send_json(400, {"error": "incomplete_body"})
                return
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self.send_json(400, {"error": "invalid_json"})
            return
        except (TimeoutError, OSError):
            self.send_json(408, {"error": "body_timeout"})
            return
        if not isinstance(data, dict) or set(data) != {"sensor_id", "request_id", "temperature_c"}:
            self.send_json(400, {"error": "invalid_fields"})
            return
        for field in ("sensor_id", "request_id"):
            if not isinstance(data[field], str) or not IDENTIFIER.fullmatch(data[field]):
                self.send_json(400, {"error": "invalid_" + field})
                return
        value = data["temperature_c"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not -273.15 <= value <= 1000 or not math.isfinite(value):
            self.send_json(400, {"error": "invalid_temperature"})
            return
        with self.server.count_lock:
            self.server.count += 1
            sequence = self.server.count
        self.send_json(201, {
            "status": "accepted",
            "sensor_id": data["sensor_id"],
            "request_id": data["request_id"],
            "temperature_c": value,
            "temperature_k": round(value + 273.15, 6),
            "observed_source_ip": peer,
            "sequence": sequence,
            "received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })


if __name__ == "__main__":
    host = os.environ.get("EDGE_BIND", "0.0.0.0")
    upf_ip = os.environ.get("UPF_IP", "172.18.0.7")
    server = EdgeServer((host, 8080), upf_ip)
    print("Edge HTTP sluzba pocuva na porte 8080; ocakavany zdroj UPF: " + upf_ip, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
