#!/usr/bin/env python3
"""Measure the running UE -> gNB -> UPF data path using host iperf3."""
import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import pwd
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
SERVER_IP = "10.45.0.1"
PORT = 5202
IPERF3_VERSION = "3.21"
IPERF3 = "iperf3"
ENV = dict(os.environ, LC_ALL="C")


class LabError(Exception):
    pass


def stop_process(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=2)


def wait_process(proc, timeout, cancel_event=None):
    if cancel_event is None:
        return proc.wait(timeout=timeout)
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if cancel_event.is_set():
            raise LabError("Zataz bola zrusena.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        try:
            proc.wait(timeout=min(.1, remaining))
        except subprocess.TimeoutExpired:
            pass
    return proc.returncode


def command(argv, timeout=15, output=None, errors=None, cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise LabError("Zataz bola zrusena.")
    with open(output, "w") if output else tempfile.TemporaryFile(mode="w+") as out:
        with open(errors, "w") if errors else tempfile.TemporaryFile(mode="w+") as err:
            proc = subprocess.Popen(argv, stdout=out, stderr=err, env=ENV, start_new_session=True)
            try:
                wait_process(proc, timeout, cancel_event)
            except subprocess.TimeoutExpired:
                stop_process(proc)
                raise LabError(f"Casovy limit ({timeout} s): " + shlex.join(list(map(str, argv))))
            except BaseException:
                stop_process(proc)
                raise
            if proc.returncode:
                err.flush()
                if errors:
                    detail = Path(errors).read_text(errors="replace")[-500:]
                else:
                    err.seek(0)
                    detail = err.read()[-500:]
                if not detail.strip() and output:
                    try:
                        failed_output = Path(output).read_text(errors="replace")
                        detail = str(json.loads(failed_output).get("error", "")) or failed_output[-500:]
                    except (ValueError, OSError, AttributeError):
                        detail = "Pozrite " + str(output)
                raise LabError(f"Prikaz zlyhal (kod {proc.returncode}): {detail.strip()}")
            if output:
                return ""
            out.seek(0)
            return out.read().strip()


def dc(*args, **kwargs):
    return command(["docker", "compose", "--project-directory", str(ROOT),
                    "-f", str(ROOT / "compose.yaml"), *args], **kwargs)


def namespace(pid, *args):
    return ["nsenter", "--no-fork", "-t", str(pid), "-n", "--", *map(str, args)]


def select_iperf3(candidate=None):
    if candidate is None:
        uid = int(os.environ.get("SUDO_UID", os.getuid()))
        user_home = Path(pwd.getpwuid(uid).pw_dir)
        candidate = user_home / ".local/open5gs-lab" / f"iperf3-{IPERF3_VERSION}/bin/iperf3"
    binary = Path(candidate).expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise LabError("Chyba izolovany iperf3: " + str(binary) +
                       ". Ako bezny pouzivatel spustite bash scripts/setup-lab-iperf3.sh.")
    version = command([str(binary), "--version"])
    if not re.match(r"^iperf " + re.escape(IPERF3_VERSION) + r"(?:\s|$)", version):
        first_line = (version.splitlines() or ["prazdny vystup"])[0]
        raise LabError(f"Ocakavana verzia iperf3 je {IPERF3_VERSION}; zistene: " + first_line)
    return str(binary), version


def container(service):
    ids = dc("ps", "-q", service).splitlines()
    if len(ids) != 1:
        raise LabError(f"Sluzba {service} musi mat jeden spusteny kontajner. Spustite compose-up.sh.")
    fields = command(["docker", "inspect", "-f",
                      "{{.State.Pid}} {{.State.Running}} {{.State.Restarting}} {{.Image}}", ids[0]]).split()
    if len(fields) != 4 or fields[1:3] != ["true", "false"] or not fields[0].isdigit() or int(fields[0]) <= 0:
        raise LabError(f"Kontajner {service} nie je pripraveny.")
    return {"id": ids[0], "pid": int(fields[0]), "image_id": fields[3]}


def number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LabError("Neplatna metrika: " + label)
    if value < 0 or (positive and value <= 0):
        raise LabError("Neplatna metrika: " + label)
    return value


def metrics(client, server, protocol, direction, duration):
    for name, data in [("client", client), ("server", server)]:
        if data.get("error") or not isinstance(data.get("end"), dict):
            raise LabError(f"Nedokonceny iperf3 {name}: {data.get('error', 'chyba end')}")
    receiver = server if direction == "UL" else client
    sender = client if direction == "UL" else server
    row = {"protocol": protocol, "direction": direction, "tcp_retransmits": None,
           "udp_jitter_ms": None, "udp_lost_packets": None,
           "udp_packets": None, "udp_loss_percent": None}
    if protocol == "TCP":
        received = receiver["end"].get("sum_received", {})
        received_bytes = number(received.get("bytes"), "TCP bytes", positive=True)
        seconds = number(received.get("seconds"), "TCP seconds", positive=True)
        retransmits = sender["end"].get("sum_sent", {}).get("retransmits")
        row["tcp_retransmits"] = number(retransmits, "TCP retransmits")
    else:
        # iperf3 3.9 end.sum mixes sender throughput with receiver loss/jitter.
        # Local receiver intervals contain its actual received bytes and timing.
        intervals = [i.get("sum", {}) for i in receiver.get("intervals", [])
                     if not i.get("sum", {}).get("omitted", False)]
        if not intervals or any(i.get("sender") is True for i in intervals):
            raise LabError("Chybaju intervaly prijimaca UDP.")
        received_bytes = sum(number(i.get("bytes"), "UDP interval bytes") for i in intervals)
        seconds = sum(number(i.get("seconds"), "UDP interval seconds", positive=True) for i in intervals)
        number(received_bytes, "UDP received bytes", positive=True)
        candidates = [receiver["end"].get("sum_received"), receiver["end"].get("sum")]
        candidates.extend(s.get("udp") for s in receiver["end"].get("streams", []))
        summary = next((s for s in candidates if isinstance(s, dict) and
                        all(k in s for k in ["jitter_ms", "lost_packets", "packets"])), None)
        if summary is None:
            raise LabError("Chybaju zaverecne statistiky prijimaca UDP.")
        lost = number(summary["lost_packets"], "UDP lost packets")
        packets = number(summary["packets"], "UDP packets", positive=True)
        if lost > packets:
            raise LabError("Pocet stratenych paketov presahuje pocet paketov UDP.")
        row.update(udp_jitter_ms=number(summary["jitter_ms"], "UDP jitter"),
                   udp_lost_packets=lost, udp_packets=packets,
                   udp_loss_percent=100 * lost / packets)
    if not 0.8 * duration <= seconds <= 2 * duration:
        raise LabError(f"Neocakavane trvanie merania {seconds:.3f} s pre zadanych {duration} s.")
    row.update(receiver_bytes=received_bytes, receiver_seconds=seconds,
               receiver_mbps=received_bytes * 8 / seconds / 1_000_000)
    return row


def ping_metrics(text):
    counts = re.search(r"(\d+) packets transmitted, (\d+) received, ([\d.]+)% packet loss", text)
    rtt = re.search(r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", text)
    if not counts or not rtt:
        raise LabError("Neda sa precitat statistika ping.")
    return dict(zip(["rtt_min_ms", "rtt_avg_ms", "rtt_max_ms", "rtt_mdev_ms"], map(float, rtt.groups())),
                packets_sent=int(counts[1]), packets_received=int(counts[2]), loss_percent=float(counts[3]))


def aggregate(rows):
    groups = []
    for protocol in ["TCP", "UDP"]:
        for direction in ["UL", "DL"]:
            subset = [r for r in rows if r["protocol"] == protocol and r["direction"] == direction]
            if not subset:
                continue
            rates = [r["receiver_mbps"] for r in subset]
            group = dict(protocol=protocol, direction=direction, completed_runs=len(subset),
                         median_receiver_mbps=statistics.median(rates),
                         min_receiver_mbps=min(rates), max_receiver_mbps=max(rates))
            if protocol == "TCP":
                group["total_retransmits"] = sum(r["tcp_retransmits"] for r in subset)
            else:
                lost = sum(r["udp_lost_packets"] for r in subset)
                packets = sum(r["udp_packets"] for r in subset)
                group.update(total_lost_packets=lost, total_packets=packets,
                             pooled_loss_percent=100 * lost / packets,
                             median_reported_jitter_ms=statistics.median(r["udp_jitter_ms"] for r in subset))
            groups.append(group)
    return groups


def write_results(folder, metadata, rows, error):
    groups = aggregate(rows)
    result = dict(metadata=metadata, status="failed" if error else "complete", error=error,
                  completed_runs=len(rows), planned_runs=4 * metadata["repeats"], runs=rows, groups=groups)
    (folder / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    if rows:
        with (folder / "runs.csv").open("w", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    lines = ["# Meranie Compose labu", "", "Stav: " + result["status"],
             f"Dokoncene merania: {len(rows)}/{result['planned_runs']}", "",
             f"Trasa: UE -> gNB -> UPF, server {metadata['server_ip']}, jeden datovy tok.",
             f"Ciel TCP: {metadata['tcp_rate']}; ciel UDP: {metadata['udp_rate']}; trvanie: {metadata['duration']} s.",
             "Ide o meranie pri zadanej zatazi. Pri TCP 0 je obmedzenie bitrate vypnute.", "",
             "| Protokol | Smer | Behy | Median prijimaca (Mbit/s) | Min | Max | Straty UDP (%) | Median jitter UDP (ms) |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for g in groups:
        loss = f"{g['pooled_loss_percent']:.4f}" if g["protocol"] == "UDP" else "-"
        jitter = f"{g['median_reported_jitter_ms']:.4f}" if g["protocol"] == "UDP" else "-"
        lines.append(f"| {g['protocol']} | {g['direction']} | {g['completed_runs']} | {g['median_receiver_mbps']:.3f} | "
                     f"{g['min_receiver_mbps']:.3f} | {g['max_receiver_mbps']:.3f} | {loss} | {jitter} |")
    lines.extend(["", "UL = UE -> UPF; DL = UPF -> UE. Straty UDP su vypocitane zo suctu paketov v danej skupine.",
                  "Jitter je median zaverecnych hodnot hlasenych prijimacom v jednotlivych behoch."])
    if metadata.get("idle_ping"):
        p = metadata["idle_ping"]
        lines.extend(["", f"ICMP RTT pred zatazou na UPF: priemer {p['rtt_avg_ms']:.3f} ms, "
                      f"maximum {p['rtt_max_ms']:.3f} ms, straty {p['loss_percent']:.2f}%."])
    if error:
        lines.extend(["", "Chyba: " + error])
    (folder / "report.md").write_text("\n".join(lines) + "\n")


def trial(ue, upf, ue_ip, folder, protocol, direction, run, args):
    cancel_event = getattr(args, 'cancel_event', None)
    label = f"{protocol.lower()}-{direction.lower()}-{run:02d}"
    paths = {name: folder / f"{label}-{name}" for name in
             ["client.json", "client.stderr", "server.json", "server.stderr"]}
    if command(namespace(upf["pid"], "ss", "-H", "-ltn", f"sport = :{PORT}"), cancel_event=cancel_event):
        raise LabError(f"Port {PORT} v UPF je obsadeny. Pozrite existujuci proces iperf3 v tomto namespace.")
    rate = args.tcp_rate if protocol == "TCP" else args.udp_rate
    client_args = [IPERF3, "-4", "-c", SERVER_IP, "-p", str(PORT), "-B", ue_ip,
                   "-t", str(args.duration), "-P", "1", "-b", rate, "--connect-timeout", "3000", "-J"]
    if protocol == "UDP":
        client_args += ["-u", "-l", "1200"]
    if direction == "DL":
        client_args += ["-R"]
    server_args = ["timeout", "--signal=TERM", "--kill-after=2s", str(args.duration + 20) + "s",
                   *namespace(upf["pid"], IPERF3, "-4", "-s", "-B", SERVER_IP, "-p", PORT, "-1", "-J")]
    (folder / f"{label}-commands.json").write_text(json.dumps(dict(server=server_args,
                                            client=namespace(ue["pid"], *client_args)), indent=2) + "\n")
    with paths["server.json"].open("w") as out, paths["server.stderr"].open("w") as err:
        server = subprocess.Popen(server_args, stdout=out, stderr=err, env=ENV, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise LabError("Server iperf3 sa ukoncil: " + paths["server.stderr"].read_text()[-400:])
                if command(namespace(upf["pid"], "ss", "-H", "-ltn", f"sport = :{PORT}"), cancel_event=cancel_event):
                    break
                time.sleep(0.2)
            else:
                raise LabError("Server iperf3 nezacal pocuvat do 5 sekund.")
            timing = dict(monotonic_start=time.monotonic(), epoch_start=time.time())
            timing_path = folder / f"{label}-client-timing.json"
            try:
                command(namespace(ue["pid"], *client_args), timeout=args.duration + 10,
                        output=paths["client.json"], errors=paths["client.stderr"], cancel_event=cancel_event)
            finally:
                timing.update(monotonic_end=time.monotonic(), epoch_end=time.time())
                timing_path.write_text(json.dumps(timing, indent=2) + "\n")
            try:
                wait_process(server, 5, cancel_event)
            except subprocess.TimeoutExpired:
                raise LabError("Server iperf3 sa po merani neukoncil.")
            if server.returncode:
                raise LabError(f"Server iperf3 sa skoncil s kodom {server.returncode}.")
        finally:
            stop_process(server)
    try:
        client_json = json.loads(paths["client.json"].read_text())
        server_json = json.loads(paths["server.json"].read_text())
    except (ValueError, OSError) as exc:
        raise LabError("Neplatny vystup JSON iperf3: " + str(exc))
    row = metrics(client_json, server_json, protocol, direction, args.duration)
    row.update(run=run, requested_rate=rate, requested_seconds=args.duration,
               client_json=paths["client.json"].name, server_json=paths["server.json"].name,
               client_timing_json=timing_path.name)
    return row


def main():
    global IPERF3
    parser = argparse.ArgumentParser(description="Meranie aktivneho Compose labu, vysledky v results/iperf-*.")
    parser.add_argument("--duration", type=int, default=5, help="sekundy na test (1-30, predvolene 5)")
    parser.add_argument("--repeats", type=int, default=3, help="opakovania kazdeho smeru a protokolu (1-10)")
    parser.add_argument("--tcp-rate", default="50M", help="ciel TCP, 0 vypne obmedzenie (predvolene 50M)")
    parser.add_argument("--udp-rate", default="5M", help="kladny ciel UDP (predvolene 5M)")
    parser.add_argument("--iperf3", type=Path, help="explicitna cesta k binarke iperf3 3.21")
    args = parser.parse_args()
    if not 1 <= args.duration <= 30 or not 1 <= args.repeats <= 10:
        parser.error("duration musi byt 1-30 a repeats 1-10")
    for name, rate in [("TCP", args.tcp_rate), ("UDP", args.udp_rate)]:
        if not re.fullmatch(r"(?:0|[1-9][0-9]*[KMG]?)", rate) or (name == "UDP" and rate == "0"):
            parser.error(f"Neplatny ciel {name}: {rate}")
    if os.geteuid() != 0:
        parser.error("Spustite: sudo python3 scripts/measure-compose.py")
    for tool in ["docker", "nsenter", "ip", "ss", "ping", "timeout"]:
        if not shutil.which(tool):
            parser.error(f"Chyba {tool}.")
    if not (ROOT / "compose.yaml").is_file():
        parser.error("Skript musi byt v scripts/ vedla projektu compose.yaml.")
    owner = (int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])) if os.environ.get("SUDO_UID") else None
    os.umask(0o077)
    results = ROOT / "results"
    created = not results.exists()
    results.mkdir(exist_ok=True)
    if created and owner:
        os.chown(results, *owner)
    lockpath = results / ".compose-up.lock"
    new_lock = not lockpath.exists()
    lock = lockpath.open("a")
    if new_lock and owner:
        os.chown(lockpath, *owner)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("Uz prebieha spustanie alebo meranie tohto labu.")
    folder = Path(tempfile.mkdtemp(prefix="iperf-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=results))
    rows, error, exit_code = [], None, 0
    metadata = dict(timestamp=dt.datetime.now().astimezone().isoformat(), duration=args.duration,
                    repeats=args.repeats, tcp_rate=args.tcp_rate, udp_rate=args.udp_rate,
                    server_ip=SERVER_IP, server_port=PORT, udp_payload_bytes=1200,
                    method="host iperf3 in UE and UPF container network namespaces", kernel=platform.release(),
                    script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    compose_sha256=hashlib.sha256((ROOT / "compose.yaml").read_bytes()).hexdigest())
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    old_term = signal.signal(signal.SIGTERM, interrupt)
    try:
        print("Meranie Compose labu. Vysledky: " + str(folder), flush=True)
        IPERF3, metadata["iperf3_version"] = select_iperf3(args.iperf3)
        metadata["iperf3_path"] = IPERF3
        metadata["iperf3_binary_sha256"] = hashlib.sha256(Path(IPERF3).read_bytes()).hexdigest()
        print("Pouzivam iperf3 " + IPERF3_VERSION + ": " + IPERF3, flush=True)
        command(["docker", "info", "--format", "{{.ServerVersion}}"])
        dc("config", "-q")
        ue, upf = container("ue"), container("upf")
        metadata.update(ue=ue, upf=upf)
        addr = command(namespace(ue["pid"], "ip", "-o", "-4", "addr", "show", "dev", "uesimtun0"))
        ips = re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)/", addr)
        if len(ips) != 1:
            raise LabError("UE nema jednu IPv4 adresu na uesimtun0. Spustite compose-up.sh.")
        ue_ip = ips[0]
        metadata["ue_ip"] = ue_ip
        route = command(namespace(ue["pid"], "ip", "-4", "route", "get", SERVER_IP, "from", ue_ip))
        (folder / "route.txt").write_text(route + "\n")
        if not re.search(r"\bdev uesimtun0\b", route):
            raise LabError("Trasa na UPF nevedie cez uesimtun0; meranie bolo zastavene.")
        command(namespace(ue["pid"], "ping", "-I", "uesimtun0", "-c", "20", "-i", "0.2", "-W", "2", "-w", "10", SERVER_IP),
                output=folder / "ping-upf.txt", errors=folder / "ping-upf.stderr")
        ping = (folder / "ping-upf.txt").read_text()
        metadata["idle_ping"] = ping_metrics(ping)
        if metadata["idle_ping"]["loss_percent"] != 0:
            raise LabError("Ping na UPF pred meranim ma straty. Pozrite ping-upf.txt.")
        for run in range(1, args.repeats + 1):
            for protocol, direction in [("TCP", "UL"), ("TCP", "DL"), ("UDP", "UL"), ("UDP", "DL")]:
                if container("ue") != ue or container("upf") != upf:
                    raise LabError("UE alebo UPF sa pocas merania zmenil. Cast merani je neplatna.")
                metadata["current_test"] = dict(protocol=protocol, direction=direction, run=run)
                print(f"{run}/{args.repeats}: {protocol} {direction}, {args.duration} s", flush=True)
                row = trial(ue, upf, ue_ip, folder, protocol, direction, run, args)
                rows.append(row)
                print(f"  prijimac: {row['receiver_mbps']:.3f} Mbit/s", flush=True)
                metadata.pop("current_test", None)
    except KeyboardInterrupt:
        error, exit_code = "Meranie bolo prerusene.", 130
    except Exception as exc:
        error, exit_code = str(exc), 1
    finally:
        signal.signal(signal.SIGTERM, old_term)
        write_results(folder, metadata, rows, error)
        (folder / "exit-code.txt").write_text(str(exit_code) + "\n")
        try:
            dc("ps", "--all", output=folder / "compose-status.txt", timeout=10)
            dc("logs", "--no-color", "--tail=200", "ue", "gnb", "amf", "smf", "upf",
               output=folder / "containers.log", timeout=10)
        except (LabError, OSError):
            pass
        if owner:
            for item in folder.rglob("*"):
                os.chown(item, *owner)
            os.chown(folder, *owner)
        lock.close()
    if error:
        print("CHYBA: " + error, file=sys.stderr)
    else:
        print("HOTOVO: " + str(len(rows)) + " merani. Pozrite report.md a runs.csv.")
        print((folder / "report.md").read_text())
    print("Vysledky: " + str(folder))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
