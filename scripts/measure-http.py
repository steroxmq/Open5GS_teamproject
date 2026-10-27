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
import signal
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


def resource_summary(samples):
    first, last = samples[0], samples[-1]
    # guest/guest_nice su uz zahrnute v user/nice.
    a, b = ([int(v) for v in s['cpu'].split()[1:9]] for s in (first, last))
    delta = [y - x for x, y in zip(a, b)]
    total = sum(delta)
    memory = []
    for sample in samples:
        fields = dict(line.split(':', 1) for line in sample['memory'].splitlines())
        memory.append({key: int(fields[key].split()[0]) for key in
                       ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree')})
    return dict(scope='hostitel VM; CPU busy zahrna steal, vylucuje idle a iowait',
                sample_count=len(samples), seconds=last['monotonic'] - first['monotonic'],
                cpu_busy_percent=100 * (total - delta[3] - delta[4]) / total
                if total > 0 and all(v >= 0 for v in delta) else None,
                mem_total_kib=memory[-1]['MemTotal'],
                min_mem_available_kib=min(s['MemAvailable'] for s in memory),
                max_swap_used_kib=max(s['SwapTotal'] - s['SwapFree'] for s in memory))


def interrupted(signum, frame):
    raise KeyboardInterrupt('Meranie bolo prerusene signalom ' + str(signum) + '.')


def overlap(rows, client, timing):
    # Datova faza dlzky D je vnutri zivota klienta [P0, P1]. Preto jej
    # zaciatok <= P1-D a koniec >= P0+D. Pouzivame spolocne monotonne hodiny,
    # nie timestamp vytvoreny pred riadiacou vymenou iperf3.
    intervals = [i['sum'] for i in client['intervals']]
    if not rows or not intervals or any(i.get('bytes', 0) <= 0 or i.get('omitted', False) for i in intervals):
        raise edge.LabError('Chybaju intervaly so skutocnym prenosom TCP.')
    previous = 0
    for interval in intervals:
        begin, end = interval['start'], interval['end']
        if not all(math.isfinite(v) for v in (begin, end)) or begin < 0 or end <= begin or abs(begin - previous) > .01:
            raise edge.LabError('Datove intervaly TCP nie su suvisle.')
        previous = end
    duration = previous - intervals[0]['start']
    lifetime = timing['monotonic_end'] - timing['monotonic_start']
    if not math.isfinite(lifetime) or lifetime <= 0 or duration > lifetime + .01:
        raise edge.LabError('Trvanie TCP nezodpoveda zivotu klienta.')
    low = timing['monotonic_end'] - duration + .1
    high = timing['monotonic_start'] + duration - .1
    for row in rows:
        row['overlap_verified'] = low <= row['monotonic_start'] < row['monotonic_end'] <= high
    if not all(r['overlap_verified'] for r in rows):
        raise edge.LabError('HTTP nie je cele v potvrdenej datovej faze TCP.')
    return dict(monotonic_lower=low, monotonic_upper=high, safety_margin_seconds=.1,
                method='Prienik garantovaneho datoveho okna z dlzky intervalov a zivota klienta.')


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
    owner = (int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])) if os.environ.get('SUDO_UID') else None
    root_existed = root.exists()
    root.mkdir(exist_ok=True)
    lockpath = root / '.compose-up.lock'
    lock_existed = lockpath.exists()
    lock = lockpath.open('a')
    if owner:
        if not root_existed:
            os.chown(root, *owner)
        if not lock_existed:
            os.chown(lockpath, *owner)
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
    old_term = signal.signal(signal.SIGTERM, interrupted)
    print('Vysledky: ' + str(folder), flush=True)
    try:
        iperf.IPERF3, summary['iperf3_version'] = iperf.select_iperf3(args.iperf3)
        summary['docker_version'] = edge.command(['docker', 'version', '--format', '{{.Server.Version}}'])
        summary['compose_version'] = edge.dc('version', '--short')
        summary['nsenter_version'] = edge.command(['nsenter', '--version'])
        summary['ip_version'] = edge.command(['ip', '-Version'])
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
        route = json.loads(edge.in_ue(fd, 'ip', '-j', '-4', 'route', 'get', edge.EDGE_IP, 'from', ue_ip))
        (folder / 'route-used.json').write_text(json.dumps(route, indent=2) + '\n')
        if len(route) != 1 or route[0].get('dev') != 'uesimtun0':
            raise edge.LabError('Trasa k edge nejde cez uesimtun0.')
        for direction in ('idle', 'UL', 'DL'):
            for repetition in range(1, args.repetitions + 1):
                label = f'{direction}-{repetition:02d}'
                group_rows = []
                group = dict(group=direction, repetition=repetition, status='failed',
                             offered_mbps=0 if direction == 'idle' else 50)
                summary['groups'].append(group)
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
                        if future and future.done():
                            future.result()
                            raise edge.LabError('TCP zataz sa skoncila pred HTTP seriou.')
                        time.sleep(max(0, schedule + index * args.interval - time.monotonic()))
                        reading = dict(sensor_id='ue-sensor-1', request_id=uuid.uuid4().hex, temperature_c=23.5)
                        row = dict(group=direction, repetition=repetition, index=index, success=False,
                                   epoch_start=time.time(), monotonic_start=time.monotonic(),
                                   elapsed_ms=None, error=None)
                        record = dict(reading=reading)
                        interruption = None
                        try:
                            raw = edge.in_ue(fd, sys.executable, str(Path(edge.__file__)), '--http-worker',
                                             stdin=json.dumps(dict(ue_ip=ue_ip, reading=reading)))
                            record['http'] = json.loads(raw)
                            row['elapsed_ms'] = record['http']['elapsed_ms']
                            edge.validate_response(record['http'], reading, ue_ip)
                            row['success'] = True
                        except (Exception, KeyboardInterrupt) as exc:
                            row['error'] = str(exc) or 'HTTP pokus bol preruseny.'
                            if isinstance(exc, KeyboardInterrupt):
                                interruption = exc
                        row['epoch_end'] = time.time()
                        row['monotonic_end'] = time.monotonic()
                        group_rows.append(row)
                        rows.append(row)
                        with (folder / 'requests.jsonl').open('a') as out:
                            out.write(json.dumps(record | row) + '\n')
                        samples.append(resources())
                        resource_path.write_text(json.dumps(samples, indent=2))
                        group.update(stats(group_rows))
                        group['resources'] = resource_summary(samples)
                        if interruption is not None:
                            raise interruption
                    if any(abs((r['epoch_end'] - r['epoch_start']) -
                               (r['monotonic_end'] - r['monotonic_start'])) > .1 for r in group_rows):
                        raise edge.LabError('Hodiny sa pocas HTTP poziadavku zmenili.')
                    offsets = [r['epoch_start'] - r['monotonic_start'] for r in group_rows]
                    if max(offsets) - min(offsets) > .1:
                        raise edge.LabError('Hodiny sa pocas HTTP serie zmenili.')
                    if future:
                        group['load'] = future.result()
                        client = json.loads((folder / group['load']['client_json']).read_text())
                        timing = json.loads((folder / group['load']['client_timing_json']).read_text())
                        group['overlap'] = overlap(group_rows, client, timing)

                    if not all(r['success'] for r in group_rows):
                        raise edge.LabError('Nie vsetky HTTP odpovede su platne.')
                for service, original in containers.items():
                    current = edge.container(service)
                    if any(current[k] != original[k] for k in ('id', 'pid', 'started_at')):
                        raise edge.LabError('Kontajner sa pocas merania obnovil.')
                group['status'] = 'complete'
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
        summary['by_condition'] = {direction: stats([r for r in rows if r['group'] == direction])
                                   for direction in ('idle', 'UL', 'DL')}
        (folder / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        with (folder / 'requests.csv').open('w') as out:
            fields = ['group', 'repetition', 'index', 'success', 'epoch_start', 'epoch_end',
                      'monotonic_start', 'monotonic_end', 'elapsed_ms', 'error', 'overlap_verified']
            writer = csv.DictWriter(out, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        (folder / 'report.md').write_text('# HTTP meranie\n\nStav: ' + summary['status'] + '\n\n' + summary['latency'] + '\n\nMetoda p95: ' + summary['percentile_method'] + '\n\n```json\n' + json.dumps(summary.get('groups'), indent=2) + '\n```\n\nChyba: ' + str(summary.get('error', summary.get('cleanup_error', 'ziadna'))) + '\n')
        if os.environ.get('SUDO_UID'):
            for item in [folder, *folder.iterdir()]:
                os.chown(item, int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))
        lock.close()
        signal.signal(signal.SIGTERM, old_term)
    print('HOTOVO.' if summary['status'] == 'complete' else
          'CHYBA: ' + summary.get('error', summary.get('cleanup_error', 'Meranie zlyhalo.')), flush=True)
    return 0 if summary['status'] == 'complete' else 1


if __name__ == '__main__':
    sys.exit(main())
