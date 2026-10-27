#!/usr/bin/env python3
"""Opakovatelne HTTP meranie bez zataze a pri TCP UL/DL."""
import argparse
import concurrent.futures
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
import uuid


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


edge = load('edge_check', 'check-edge.py')
iperf = load('lab_iperf', 'measure-compose.py')


def stats(rows):
    values = sorted(r['elapsed_ms'] for r in rows if r['success'])
    return dict(requests=len(rows), successful=len(values),
                success_percent=100 * len(values) / len(rows) if rows else 0,
                median_ms=statistics.median(values) if values else None,
                p95_ms=values[math.ceil(.95 * len(values)) - 1] if values else None)


def resources():
    return dict(epoch=time.time(), monotonic=time.monotonic(),
                cpu=Path('/proc/stat').read_text().splitlines()[0],
                memory=Path('/proc/meminfo').read_text(), load=os.getloadavg())


def overlap(rows, client):
    # iperf start timestamp has whole-second resolution. Exclude one second
    # at BOTH boundaries; do not infer data transfer from a live process.
    start = client['start']['timestamp']['timesecs']
    intervals = [i['sum'] for i in client['intervals']]
    if not intervals or any(i.get('bytes', 0) <= 0 for i in intervals):
        raise edge.LabError('Chybaju intervaly so skutocnym prenosom TCP.')
    low = start + 1
    high = start + max(i['end'] for i in intervals) - 1
    for row in rows:
        row['overlap_verified'] = low <= row['epoch_start'] < row['epoch_end'] <= high
    if not all(r['overlap_verified'] for r in rows):
        raise edge.LabError('HTTP nie je cele v potvrdenej datovej faze TCP.')
    return dict(epoch_lower=low, epoch_upper=high, timestamp_uncertainty_seconds=1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repetitions', type=int, default=3)
    p.add_argument('--requests', type=int, default=20)
    p.add_argument('--interval', type=float, default=.4)
    p.add_argument('--duration', type=int, default=16)
    p.add_argument('--iperf3', type=Path)
    args = p.parse_args()
    if args.repetitions < 1 or args.requests < 1 or not math.isfinite(args.interval) or args.interval <= 0 or args.duration < 8 or args.requests * args.interval + 7 > args.duration:
        p.error('Neplatne parametre alebo prilis kratka zataz.')
    if os.geteuid() != 0:
        p.error('Spustite: sudo python3 scripts/measure-http.py')
    root = edge.ROOT / 'results'
    root.mkdir(exist_ok=True)
    lock = (root / '.compose-up.lock').open('a')
    try:
        edge.fcntl.flock(lock, edge.fcntl.LOCK_EX | edge.fcntl.LOCK_NB)
    except BlockingIOError:
        p.error('Prebieha iny start alebo meranie labu.')
    folder = Path(tempfile.mkdtemp(prefix='http-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S-'), dir=root))
    summary = dict(status='failed', parameters=vars(args) | {'iperf3': str(args.iperf3)},
                   timestamp=dt.datetime.now().astimezone().isoformat(), groups=[],
                   python=sys.version, kernel=platform.release(),
                   script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   percentile_method='nearest rank ceil(0.95*n), iba uspesne odpovede',
                   latency='Cas celeho HTTP poziadavku: TCP connect az precitanie tela; nie RTT.')
    rows, fd, created = [], None, False
    print('Vysledky: ' + str(folder), flush=True)
    try:
        iperf.IPERF3, summary['iperf3_version'] = iperf.select_iperf3(args.iperf3)
        summary['iperf3_path'] = iperf.IPERF3
        summary['iperf3_sha256'] = hashlib.sha256(Path(iperf.IPERF3).read_bytes()).hexdigest()
        containers = summary['containers'] = {s: edge.container(s) for s in ('ue', 'upf', 'edge')}
        if containers['edge']['health'] != 'healthy' or containers['edge']['ips'] != [edge.EDGE_IP] or edge.UPF_IP not in containers['upf']['ips']:
            raise edge.LabError('Edge alebo UPF nema ocakavany stav.')
        fd = os.open('/proc/' + str(containers['ue']['pid']) + '/ns/net', os.O_RDONLY)
        addresses = json.loads(edge.in_ue(fd, 'ip', '-j', '-4', 'addr', 'show', 'dev', 'uesimtun0'))
        ips = [a['local'] for d in addresses for a in d.get('addr_info', []) if a.get('family') == 'inet']
        if len(ips) != 1 or edge.ipaddress.ip_address(ips[0]) not in edge.ipaddress.ip_network('10.45.0.0/16'):
            raise edge.LabError('Neplatna adresa UE.')
        ue_ip = summary['ue_ip'] = ips[0]
        before = json.loads(edge.in_ue(fd, 'ip', '-j', '-4', 'route', 'show', 'exact', edge.TARGET))
        (folder / 'route-before.json').write_text(json.dumps(before))
        if before:
            if len(before) != 1 or before[0].get('dev') != 'uesimtun0' or before[0].get('prefsrc') != ue_ip:
                raise edge.LabError('Existujuca trasa sa nesmie prepisat.')
        else:
            created = True
            edge.in_ue(fd, 'ip', 'route', 'add', edge.TARGET, 'dev', 'uesimtun0', 'src', ue_ip)
        for direction in ('idle', 'UL', 'DL'):
            for repetition in range(1, args.repetitions + 1):
                label = f'{direction}-{repetition:02d}'
                group_rows = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = None
                    if direction != 'idle':
                        opts = argparse.Namespace(tcp_rate='50M', duration=args.duration)
                        future = pool.submit(iperf.trial, containers['ue'], containers['upf'], ue_ip,
                                             folder, 'TCP', direction, repetition, opts)
                        time.sleep(3)
                    schedule = time.monotonic()
                    samples = [resources()]
                    resource_path = folder / (label + '-resources.json')
                    resource_path.write_text(json.dumps(samples, indent=2))
                    for index in range(args.requests):
                        time.sleep(max(0, schedule + index * args.interval - time.monotonic()))
                        reading = dict(sensor_id='ue-sensor-1', request_id=uuid.uuid4().hex, temperature_c=23.5)
                        row = dict(group=direction, repetition=repetition, index=index, success=False,
                                   epoch_start=time.time(), elapsed_ms=None, error=None)
                        record = dict(reading=reading)
                        try:
                            raw = edge.in_ue(fd, sys.executable, str(Path(edge.__file__)), '--http-worker',
                                             stdin=json.dumps(dict(ue_ip=ue_ip, reading=reading)))
                            record['http'] = json.loads(raw)
                            row['elapsed_ms'] = record['http']['elapsed_ms']
                            edge.validate_response(record['http'], reading, ue_ip)
                            row['success'] = True
                        except Exception as exc:
                            row['error'] = str(exc)
                        row['epoch_end'] = time.time()
                        group_rows.append(row)
                        rows.append(row)
                        with (folder / 'requests.jsonl').open('a') as out:
                            out.write(json.dumps(record | row) + '\n')
                        samples.append(resources())
                        resource_path.write_text(json.dumps(samples, indent=2))
                    group = dict(group=direction, repetition=repetition, offered_mbps=0 if not future else 50)
                    summary['groups'].append(group)
                    if future:
                        group['load'] = future.result()
                        client = json.loads((folder / group['load']['client_json']).read_text())
                        group['overlap'] = overlap(group_rows, client)
                    group.update(stats(group_rows))

                    if not all(r['success'] for r in group_rows):
                        raise edge.LabError('Nie vsetky HTTP odpovede su platne.')
                for service, original in containers.items():
                    current = edge.container(service)
                    if any(current[k] != original[k] for k in ('id', 'pid', 'started_at')):
                        raise edge.LabError('Kontajner sa pocas merania obnovil.')
        summary['status'] = 'complete'
    except (Exception, KeyboardInterrupt) as exc:
        summary['error'] = str(exc) or 'Meranie bolo prerusene.'
    finally:
        if created and fd is not None:
            try:
                edge.in_ue(fd, 'ip', 'route', 'del', edge.TARGET, 'dev', 'uesimtun0', 'src', summary['ue_ip'])
            except Exception as exc:
                summary.update(status='failed', cleanup_error=str(exc))
        if fd is not None:
            os.close(fd)
        summary['http'] = stats(rows)
        (folder / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        with (folder / 'requests.csv').open('w') as out:
            fields = ['group', 'repetition', 'index', 'success', 'epoch_start', 'epoch_end', 'elapsed_ms', 'error', 'overlap_verified']
            writer = csv.DictWriter(out, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        (folder / 'report.md').write_text('# HTTP meranie\n\nStav: ' + summary['status'] + '\n\n' + summary['latency'] + '\n\nMetoda p95: ' + summary['percentile_method'] + '\n\n```json\n' + json.dumps(summary.get('groups'), indent=2) + '\n```\n\nChyba: ' + str(summary.get('error', summary.get('cleanup_error', 'ziadna'))) + '\n')
        if os.environ.get('SUDO_UID'):
            for item in [folder, *folder.iterdir()]:
                os.chown(item, int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))
        lock.close()
    return 0 if summary['status'] == 'complete' else 1


if __name__ == '__main__':
    sys.exit(main())
