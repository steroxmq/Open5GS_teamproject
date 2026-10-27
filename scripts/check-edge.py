#!/usr/bin/env python3
"""Send a sensor POST from UE through its TUN and record the UPF NAT source."""
import argparse
import datetime as dt
import fcntl
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
EDGE_IP, UPF_IP, PORT = "172.18.0.30", "172.18.0.7", 8080
TARGET = EDGE_IP + "/32"


class LabError(Exception):
    pass


class CommandTimeout(LabError):
    pass


def interrupted(signum, frame):
    raise KeyboardInterrupt("Test bol preruseny signalom " + str(signum) + ".")


def command(argv, timeout=15, stdin=None, pass_fds=()):
    try:
        proc = subprocess.run(list(map(str, argv)), input=stdin, capture_output=True,
                              text=True, timeout=timeout, pass_fds=pass_fds,
                              env=dict(os.environ, LC_ALL="C"))
    except subprocess.TimeoutExpired as exc:
        raise CommandTimeout("Casovy limit prikazu: " + str(argv[0])) from exc
    if proc.returncode:
        raise LabError("Prikaz zlyhal: " + (proc.stderr or proc.stdout).strip()[-700:])
    return proc.stdout.strip()


def dc(*args):
    return command(["docker", "compose", "--project-directory", ROOT,
                    "-f", ROOT / "compose.yaml", "-f", ROOT / "compose.edge.yaml", *args])


def container(service):
    ids = dc("ps", "-q", service).splitlines()
    if len(ids) != 1:
        raise LabError("Sluzba " + service + " musi mat jeden spusteny kontajner.")
    info = json.loads(command(["docker", "inspect", ids[0]]))[0]
    if not info["State"]["Running"] or info["State"]["Restarting"] or info["State"]["Pid"] <= 0:
        raise LabError("Kontajner " + service + " nie je pripraveny.")
    return {"id": info["Id"], "pid": info["State"]["Pid"], "image_id": info["Image"],
            "started_at": info["State"]["StartedAt"],
            "ips": sorted(n["IPAddress"] for n in info["NetworkSettings"]["Networks"].values()),
            "health": info["State"].get("Health", {}).get("Status")}


def in_ue(net_fd, *args, stdin=None):
    # Holding a descriptor pins this namespace even if its original PID exits.
    # Only the network namespace is entered; host binaries/files remain visible.
    return command(["nsenter", "--no-fork", "--net=/proc/self/fd/" + str(net_fd),
                    "--", *args], stdin=stdin, pass_fds=(net_fd,))


def http_worker():
    data = json.loads(sys.stdin.read())
    ipaddress.IPv4Address(data["ue_ip"])
    payload = json.dumps(data["reading"], allow_nan=False).encode()
    started = time.monotonic()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"uesimtun0\0")
        sock.bind((data["ue_ip"], 0))
        sock.connect((EDGE_IP, PORT))
        local = sock.getsockname()[0]
        conn = http.client.HTTPConnection(EDGE_IP, PORT, timeout=5)
        conn.sock = sock
        try:
            conn.request("POST", "/readings", body=payload,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            body = response.read(16385)
            if len(body) > 16384:
                raise LabError("Odpoved HTTP je prilis velka.")
            result = {"http_status": response.status, "client_source_ip": local,
                      "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                      "response": json.loads(body)}
        finally:
            conn.close()
    print(json.dumps(result, allow_nan=False))


def validate_response(result, reading, ue_ip):
    if not isinstance(result, dict) or not isinstance(result.get("response"), dict):
        raise LabError("HTTP odpoved nema ocakavanu strukturu JSON.")
    body = result["response"]
    elapsed = result.get("elapsed_ms")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
        raise LabError("Neplatne trvanie HTTP poziadavku.")
    if result.get("http_status") != 201 or body.get("status") != "accepted":
        raise LabError("Edge sluzba neprijala udaj: " + json.dumps(result))
    if result.get("client_source_ip") != ue_ip or body.get("observed_source_ip") != UPF_IP:
        raise LabError("Odpoved nepotvrdzuje zdroj UE a NAT adresu UPF.")
    for field in ("request_id", "sensor_id", "temperature_c"):
        if body.get(field) != reading[field]:
            raise LabError("Nezhoduje sa udaj v odpovedi: " + field)
    kelvin = body.get("temperature_k")
    if isinstance(kelvin, bool) or not isinstance(kelvin, (int, float)) or not math.isfinite(kelvin) or not math.isclose(kelvin, reading["temperature_c"] + 273.15, abs_tol=1e-6):
        raise LabError("Nespravny prevod teploty v edge aplikacii.")


def write_report(folder, summary):
    (folder / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    lines = ["# Overenie HTTP edge sluzby", "", "Stav: " + summary["status"], ""]
    if summary.get("error"):
        lines += ["Chyba: " + summary["error"], ""]
    if isinstance(summary.get("http"), dict):
        http = summary["http"]
        body = http.get("response") if isinstance(http.get("response"), dict) else {}
        lines += ["- HTTP: " + str(http.get("http_status", "?")),
                  "- Zdroj UE: " + str(http.get("client_source_ip", "?")),
                  "- Zdroj pozorovany aplikaciou po NAT: " + str(body.get("observed_source_ip", "?")),
                  "- Cas jedneho HTTP poziadavku: " + str(http.get("elapsed_ms", "?")) + " ms", ""]
    lines += ["Trasa bola overovana s vazbou socketu na uesimtun0 a s adresou UE.",
              "Kontroloval sa aj zdroj UPF pozorovany aplikaciou a obsah odpovede.",
              "Jeden HTTP poziadavok nie je seriou merani QoS ani dokazom realnej latencie MEC."]
    (folder / "report.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Overenie UE -> gNB -> UPF -> HTTP edge sluzba.")
    parser.add_argument("--temperature", type=float, default=23.5, help="Teplota v C (predvolene 23.5).")
    args = parser.parse_args()
    if not math.isfinite(args.temperature) or not -273.15 <= args.temperature <= 1000:
        parser.error("Teplota musi byt konecne cislo od -273.15 do 1000.")
    if os.geteuid() != 0:
        parser.error("Spustite: sudo python3 scripts/check-edge.py")
    for tool in ("docker", "nsenter", "ip"):
        if not shutil.which(tool):
            parser.error("Chyba prikaz: " + tool)
    if not (ROOT / "compose.edge.yaml").is_file():
        parser.error("Chyba compose.edge.yaml v koreni projektu.")

    owner = (int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])) if os.environ.get("SUDO_UID") else None
    results = ROOT / "results"
    existed = results.exists()
    results.mkdir(mode=0o700, exist_ok=True)
    if owner and not existed:
        os.chown(results, *owner)
    lockpath = results / ".compose-up.lock"
    lock_new = not lockpath.exists()
    lock = lockpath.open("a")
    if owner and lock_new:
        os.chown(lockpath, *owner)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        parser.error("Prebieha iny start alebo meranie labu. Pockajte na jeho ukoncenie.")
    folder = Path(tempfile.mkdtemp(prefix="edge-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=results))
    summary = {"status": "failed", "timestamp": dt.datetime.now().astimezone().isoformat(),
               "test": "one_sensor_http_request", "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "edge_ip": EDGE_IP, "upf_nat_ip": UPF_IP, "error": None}
    fd = None
    route_created = False
    exit_code = 1
    old_term = signal.signal(signal.SIGTERM, interrupted)
    print("Overenie edge sluzby. Vysledky: " + str(folder), flush=True)
    try:
        for service in ("ue", "upf", "edge"):
            summary[service] = container(service)
        if summary["edge"]["ips"] != [EDGE_IP] or UPF_IP not in summary["upf"]["ips"]:
            raise LabError("Adresy edge alebo UPF nezodpovedaju tejto konfiguracii.")
        if summary["edge"]["health"] != "healthy":
            raise LabError("Edge este nie je healthy. Skontrolujte Compose ps a skuste znova.")
        fd = os.open("/proc/" + str(summary["ue"]["pid"]) + "/ns/net", os.O_RDONLY)
        addresses = json.loads(in_ue(fd, "ip", "-j", "-4", "addr", "show", "dev", "uesimtun0"))
        ue_ips = [a["local"] for dev in addresses for a in dev.get("addr_info", []) if a.get("family") == "inet"]
        if len(ue_ips) != 1 or ipaddress.IPv4Address(ue_ips[0]) not in ipaddress.IPv4Network("10.45.0.0/16"):
            raise LabError("UE nema ocakavanu IPv4 na uesimtun0. Spustite compose-up.sh.")
        ue_ip = summary["ue_ip"] = ue_ips[0]
        before = json.loads(in_ue(fd, "ip", "-j", "-4", "route", "show", "exact", TARGET))
        (folder / "ue-route-before.json").write_text(json.dumps(before, indent=2) + "\n")
        if before:
            if len(before) != 1 or before[0].get("dev") != "uesimtun0" or before[0].get("prefsrc") != ue_ip:
                raise LabError("Pre edge uz existuje ina /32 trasa; skript ju nebude prepisovat.")
        else:
            # Set before calling ip so cleanup is attempted even after a CLI timeout.
            route_created = True
            try:
                in_ue(fd, "ip", "-4", "route", "add", TARGET, "dev", "uesimtun0", "src", ue_ip)
            except CommandTimeout:
                raise
            except (LabError, OSError):
                # Odmietnute add nic nevytvorilo; neodstranit cudziu trasu.
                route_created = False
                raise
        route = json.loads(in_ue(fd, "ip", "-j", "-4", "route", "get", EDGE_IP, "from", ue_ip))
        (folder / "ue-route-used.json").write_text(json.dumps(route, indent=2) + "\n")
        if len(route) != 1 or route[0].get("dev") != "uesimtun0":
            raise LabError("Trasa k edge nejde cez uesimtun0.")
        reading = {"sensor_id": "ue-sensor-1", "request_id": uuid.uuid4().hex,
                   "temperature_c": args.temperature}
        summary["reading"] = reading
        response = in_ue(fd, sys.executable, str(Path(__file__).resolve()), "--http-worker",
                         stdin=json.dumps({"ue_ip": ue_ip, "reading": reading}))
        summary["http"] = json.loads(response)
        validate_response(summary["http"], reading, ue_ip)
        for service in ("ue", "upf", "edge"):
            current = container(service)
            if any(current[k] != summary[service][k] for k in ("id", "pid", "started_at")):
                raise LabError("Kontajner " + service + " sa pocas testu obnovil; vysledok nie je platny.")
        summary["status"] = "complete"
        exit_code = 0
    except (LabError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as exc:
        summary["status"] = "failed"
        summary["error"] = str(exc)
        print("CHYBA: " + str(exc), file=sys.stderr)
    except KeyboardInterrupt:
        summary["status"] = "failed"
        summary["error"] = "Test bol preruseny."
        exit_code = 130
    finally:
        old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if route_created and fd is not None:
            try:
                in_ue(fd, "ip", "-4", "route", "del", TARGET, "dev", "uesimtun0", "src", summary["ue_ip"])
                summary["temporary_route_removed"] = True
            except (LabError, OSError) as exc:
                summary["temporary_route_removed"] = False
                summary["status"] = "failed"
                summary["error"] = (summary["error"] or "") + " Obnova trasy zlyhala: " + str(exc)
                exit_code = 1
        if fd is not None:
            os.close(fd)
        try:
            (folder / "containers.log").write_text(dc("logs", "--no-color", "--tail=80", "edge", "ue", "upf") + "\n")
        except (LabError, OSError) as exc:
            (folder / "containers.log").write_text("Logy nie su dostupne: " + str(exc) + "\n")
        write_report(folder, summary)
        if owner:
            for item in folder.iterdir():
                os.chown(item, *owner)
            os.chown(folder, *owner)
        lock.close()
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        print("Vysledky: " + str(folder), flush=True)
    if exit_code == 0:
        print("HOTOVO: HTTP 201, spravny udaj senzora a potvrdeny zdroj UPF.")
    return exit_code


if __name__ == "__main__":
    if sys.argv[1:] == ["--http-worker"]:
        try:
            http_worker()
        except (LabError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
    else:
        sys.exit(main())
