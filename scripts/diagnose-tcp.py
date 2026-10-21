#!/usr/bin/env python3
"""One traced TCP UL trial. Reuses measure-compose.py; does not restart the lab."""
import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import threading
import time
from types import SimpleNamespace


def probe(m, ue, upf, ue_ip, folder, duration=5, rate="50M"):
    original_namespace = m.namespace
    stopped = threading.Event()
    started = time.monotonic()
    snapshots = []

    def traced_namespace(pid, *argv):
        if argv and argv[0] == m.IPERF3:
            side = "client" if pid == ue["pid"] else "server"
            argv = ("strace", "-f", "--seccomp-bpf", "-ttt", "-T", "-s", "96", "-yy",
                    "-e", "trace=network,read,write,close,fcntl",
                    "-o", str(folder / (side + ".strace")), *argv)
        return original_namespace(pid, *argv)

    def sample():
        for target in (3, 7, 12):
            if stopped.wait(max(0, target - (time.monotonic() - started))):
                return
            for side, info in (("ue", ue), ("upf", upf)):
                record = dict(side=side, target_seconds=target,
                              elapsed_seconds=time.monotonic() - started)
                try:
                    m.command(original_namespace(info["pid"], "ss", "-tinp",
                              f"( sport = :{m.PORT} or dport = :{m.PORT} )"), timeout=2,
                              output=folder / f"sockets-{target:02d}s-{side}.txt",
                              errors=folder / f"sockets-{target:02d}s-{side}.stderr")
                except Exception as exc:
                    record["error"] = str(exc)
                snapshots.append(record)

    m.namespace = traced_namespace
    worker = threading.Thread(target=sample, daemon=True)
    worker.start()
    result = {"status": "failed", "purpose": "diagnostic; not a throughput benchmark",
              "client_timeout_seconds": duration + 10, "server_timeout_seconds": duration + 20}
    try:
        result["metrics"] = m.trial(ue, upf, ue_ip, folder, "TCP", "UL", 1,
                                    SimpleNamespace(duration=duration, tcp_rate=rate))
        result["status"] = "complete"
    except (Exception, KeyboardInterrupt) as exc:
        result["error"] = str(exc) or "Prerusene pouzivatelom."
    finally:
        stopped.set()
        worker.join(timeout=6)
        m.namespace = original_namespace
        result["elapsed_seconds"] = time.monotonic() - started
        result["snapshots"] = list(snapshots)
    return result


def main():
    parser = argparse.ArgumentParser(description="Jeden TCP UL pokus so strace a snimkami spojeni.")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="adresar s compose.yaml")
    parser.add_argument("--iperf3", type=Path, help="explicitna cesta k binarke iperf3 3.21")
    args = parser.parse_args()
    root = args.project.expanduser().resolve()
    source = root / "scripts/measure-compose.py"
    if os.geteuid() != 0:
        parser.error("Spustite cez sudo python3.")
    for tool in ("strace", "docker", "nsenter", "ss", "ip", "timeout"):
        if not shutil.which(tool):
            parser.error("Chyba prikaz: " + tool)
    if not source.is_file() or not (root / "compose.yaml").is_file():
        parser.error("Chyba compose.yaml alebo scripts/measure-compose.py v " + str(root))
    spec = importlib.util.spec_from_file_location("measure_compose", source)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.ROOT = root
    for name in ("trial", "command", "namespace", "container", "dc", "select_iperf3"):
        if not callable(getattr(m, name, None)):
            parser.error("Nekompatibilny measure-compose.py: chyba " + name)
    owner = (int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])) if os.environ.get("SUDO_UID") else None
    os.umask(0o077)
    results = root / "results"
    if not results.exists():
        results.mkdir()
        if owner:
            os.chown(results, *owner)
    lockpath = results / ".compose-up.lock"
    new_lock = not lockpath.exists()
    lock = lockpath.open("a")
    if new_lock and owner:
        os.chown(lockpath, *owner)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("Uz prebieha spustanie alebo meranie labu.")
    folder = Path(tempfile.mkdtemp(prefix="tcp-diagnostic-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=results))
    since = dt.datetime.now(dt.timezone.utc).isoformat()
    result = dict(status="failed", timestamp_utc=since, protocol="TCP", direction="UL",
                  duration=5, target_rate="50M", server_ip=m.SERVER_IP, port=m.PORT,
                  measurement_script_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  purpose="diagnostic; strace changes timing, do not use as a capacity measurement")
    print("Jeden diagnosticky TCP UL pokus: 5 s, 50 Mbit/s. Vysledky: " + str(folder), flush=True)
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    old_term = signal.signal(signal.SIGTERM, interrupt)
    try:
        m.IPERF3, result["iperf3_version"] = m.select_iperf3(args.iperf3)
        result["iperf3_path"] = m.IPERF3
        result["iperf3_binary_sha256"] = hashlib.sha256(Path(m.IPERF3).read_bytes()).hexdigest()
        result["strace_version"] = m.command(["strace", "--version"])
        ue, upf = m.container("ue"), m.container("upf")
        result.update(ue=ue, upf=upf)
        address = m.command(m.namespace(ue["pid"], "ip", "-j", "-4", "addr", "show", "dev", "uesimtun0"))
        ips = [a["local"] for interface in json.loads(address) for a in interface.get("addr_info", [])
               if a.get("family") == "inet"]
        if len(ips) != 1:
            raise m.LabError("UE nema jednu IPv4 adresu na uesimtun0.")
        result["ue_ip"] = ips[0]
        route = m.command(m.namespace(ue["pid"], "ip", "-j", "-4", "route", "get", m.SERVER_IP, "from", ips[0]))
        (folder / "route.json").write_text(route + "\n")
        if not json.loads(route) or any(r.get("dev") != "uesimtun0" for r in json.loads(route)):
            raise m.LabError("Trasa na UPF nevedie cez uesimtun0.")
        result.update(probe(m, ue, upf, ips[0], folder))
        for side, before in (("ue", ue), ("upf", upf)):
            result[side + "_after"] = m.container(side)
            if result[side + "_after"] != before:
                result["status"] = "failed"
                result["error"] = "Kontajner " + side + " sa pocas pokusu zmenil."
    except (Exception, KeyboardInterrupt) as exc:
        result.update(status="failed", error=str(exc) or "Prerusene pouzivatelom.")
    finally:
        signal.signal(signal.SIGTERM, old_term)
        for filename, argv in (("containers.log", ("logs", "--no-color", "--since", since, "ue", "gnb", "amf", "smf", "upf")),
                               ("compose-status.txt", ("ps", "--all"))):
            try:
                m.dc(*argv, timeout=10, output=folder / filename, errors=folder / (filename + ".stderr"))
            except Exception as exc:
                result.setdefault("collection_errors", []).append(filename + ": " + str(exc))
        (folder / "diagnostic.json").write_text(json.dumps(result, indent=2) + "\n")
        if owner:
            for path in [folder, *folder.rglob("*")]:
                os.chown(path, *owner)
        lock.close()
    archive = shutil.make_archive(str(folder), "gztar", root_dir=folder.parent, base_dir=folder.name)
    if owner:
        os.chown(archive, *owner)
    print("Stav: " + result["status"])
    if result.get("error"):
        print("Chyba: " + result["error"])
    print("ARCHIV: " + archive)
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
