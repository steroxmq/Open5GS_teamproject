#!/usr/bin/env python3
"""Kontrola cielovej Linux VM pred prenosom labu; nemenit siet ani databazu."""
import argparse
import datetime as dt
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LAB_NETWORKS = (ipaddress.ip_network('172.18.0.0/24'),
                ipaddress.ip_network('10.45.0.0/16'))


def command(args):
    """Chybove vystupy Dockeru mozu obsahovat env; nikdy ich nezapisovat."""
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('Neda sa vykonat prikaz ' + args[0]) from None
    if result.returncode:
        raise RuntimeError('Prikaz ' + args[0] + ' zlyhal; overte instalaciu a opravnenia.')
    return result.stdout.strip()


def env_errors(path):
    """Vracat iba nazvy chybnych premennych, nie ich hodnoty."""
    values, duplicates = {}, set()
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition('=')
        if key not in ('UE_KEY', 'UE_OPC', 'WEBUI_BIND_IP') or not sep:
            continue
        if key in values:
            duplicates.add(key)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    errors = ['Duplicitna premenna ' + key for key in sorted(duplicates)]
    for key in ('UE_KEY', 'UE_OPC'):
        if not re.fullmatch(r'[0-9A-Fa-f]{32}', values.get(key, '')):
            errors.append('Neplatny format ' + key)
    if values.get('WEBUI_BIND_IP', '127.0.0.1') != '127.0.0.1':
        errors.append('Serverovy profil vyzaduje WEBUI_BIND_IP=127.0.0.1')
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        errors.append('Subor .env musi mat opravnenia 600 alebo prisnejsie')
    return errors


def overlaps(cidr):
    network = ipaddress.ip_network(cidr, strict=False)
    return network.version == 4 and any(network.overlaps(lab) for lab in LAB_NETWORKS)


def network_conflicts(networks, project):
    conflicts, own_bridges = [], set()
    for network in networks:
        cidrs = [entry['Subnet'] for entry in network.get('IPAM', {}).get('Config') or []
                 if entry.get('Subnet')]
        labels = network.get('Labels') or {}
        own = (labels.get('com.docker.compose.project') == project and
               labels.get('com.docker.compose.network') == 'core' and
               cidrs == ['172.18.0.0/24'] and network.get('Driver') == 'bridge')
        if own:
            bridge = (network.get('Options') or {}).get('com.docker.network.bridge.name')
            own_bridges.add(bridge or 'br-' + network['Id'][:12])
        for cidr in cidrs:
            if overlaps(cidr) and not own:
                conflicts.append(network.get('Name', '?') + ': ' + cidr)
    return conflicts, own_bridges


def route_conflicts(routes, own_bridges):
    conflicts = []
    for route in routes:
        cidr = route.get('dst', 'default')
        if cidr in ('default', '0.0.0.0/0'):
            continue
        network = ipaddress.ip_network(cidr, strict=False)
        # Pripojena a lokalna trasa vlastneho Docker bridge su ocakavane.
        if (route.get('dev') in own_bridges and network.version == 4 and
                network.subnet_of(LAB_NETWORKS[0])):
            continue
        if overlaps(cidr):
            conflicts.append(cidr + ' dev ' + route.get('dev', '?'))
    return sorted(set(conflicts))


def local_docker_endpoint():
    context = os.environ.get('DOCKER_CONTEXT')
    if context:
        endpoint = command(['docker', 'context', 'inspect', context,
                            '--format', '{{.Endpoints.docker.Host}}'])
    elif os.environ.get('DOCKER_HOST'):
        endpoint = os.environ['DOCKER_HOST']
    else:
        endpoint = command(['docker', 'context', 'inspect',
                            '--format', '{{.Endpoints.docker.Host}}'])
    return endpoint.startswith('unix://')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Novy subor JSON; existujuci sa neprepise.')
    args = parser.parse_args()
    report = dict(timestamp=dt.datetime.now().astimezone().isoformat(),
                  kernel=platform.release(), architecture=platform.machine(), checks=[])

    def check(name, ok, detail, warning=False):
        status = 'ok' if ok else ('warning' if warning else 'failed')
        report['checks'].append(dict(name=name, status=status, detail=detail))
        print(status.upper() + ': ' + name + ': ' + detail)

    check('platforma', platform.system() == 'Linux' and platform.machine() == 'x86_64',
          'Cielovy profil: Linux x86_64 v samostatnej VM.')
    check('opravnenia', os.geteuid() == 0,
          'Spustite sudo python3 scripts/server-preflight.py; Docker aj namespace musia byt lokalne.')
    cpuinfo = Path('/proc/cpuinfo').read_text() if Path('/proc/cpuinfo').exists() else ''
    flags = re.findall(r'^flags\s*:\s*(.*)$', cpuinfo, re.MULTILINE)
    check('AVX', bool(flags) and all('avx' in value.split() for value in flags),
          'MongoDB 6.0 potrebuje AVX; v Proxmox overte CPU model VM (napr. host).')
    check('vCPU', (os.cpu_count() or 0) >= 4, str(os.cpu_count()) + '; odporucame aspon 4.', True)
    mem = Path('/proc/meminfo')
    kib = int(re.search(r'^MemTotal:\s+(\d+)', mem.read_text()).group(1)) if mem.exists() else 0
    check('RAM', kib >= 7 * 1024 * 1024, str(round(kib / 1024**2, 2)) +
          ' GiB viditelnych vo VM; odporucame pridelit 8 GiB.', True)
    tun = Path('/dev/net/tun')
    check('TUN', tun.exists() and stat.S_ISCHR(tun.stat().st_mode) and os.access(tun, os.R_OK | os.W_OK),
          '/dev/net/tun musi byt pristupne; priprava: sudo modprobe tun.')
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM, 132):
            pass
        sctp = True
    except OSError:
        sctp = False
    check('SCTP', sctp, 'Kontrola vytvorenia SCTP socketu bez pripojenia; priprava: sudo modprobe sctp.')

    missing = [tool for tool in ('docker', 'ip', 'nsenter', 'flock', 'timeout', 'ping', 'git')
               if not shutil.which(tool)]
    check('nastroje', not missing, 'Chybaju: ' + ', '.join(missing) if missing else 'Zakladne nastroje su dostupne.')
    env = ROOT / '.env'
    try:
        issues = env_errors(env) if env.is_file() else ['Chyba .env; preneste existujuci subor cez SSH.']
    except OSError:
        issues = ['Subor .env sa neda precitat; overte opravnenia.']
    check('konfiguracia UE', not issues, '; '.join(issues) if issues else 'Format a prava .env su v poriadku; kluce sa nevypisuju.')
    try:
        report['git_commit'] = command(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'])
    except RuntimeError as exc:
        check('Git', False, str(exc))
    if 'nsenter' not in missing and 'ip' not in missing:
        try:
            command(['nsenter', '--net=/proc/self/ns/net', '--', 'ip', '-j', '-4', 'link', 'show'])
            check('namespace', True, 'Vstup do lokalneho sietoveho namespace je povoleny.')
        except RuntimeError:
            check('namespace', False, 'Vstup cez nsenter je zakazany; toto je problem opravneni.')

    model, info, own_bridges = None, {}, set()
    if 'docker' not in missing:
        try:
            if not local_docker_endpoint():
                raise RuntimeError('Vzdialeny Docker nie je vhodny: merania pouzivaju lokalne PID a namespace.')
            info = json.loads(command(['docker', 'info', '--format', '{{json .}}']))
            if any('rootless' in item for item in info.get('SecurityOptions', [])):
                raise RuntimeError('Tento profil vyzaduje rootful Docker v samostatnej VM.')
            report['docker_version'] = info.get('ServerVersion')
            report['compose_version'] = command(['docker', 'compose', 'version', '--short'])
            check('Docker', True, 'Lokalny rootful daemon aj Compose odpovedaju.')
            model = json.loads(command(['docker', 'compose', '--project-directory', str(ROOT),
                                        '-f', str(ROOT / 'compose.yaml'), '-f', str(ROOT / 'compose.edge.yaml'),
                                        'config', '--format', 'json']))
            bind_errors = [service + ': ' + volume['source']
                           for service, config in model['services'].items()
                           for volume in config.get('volumes', [])
                           if volume['type'] == 'bind' and not Path(volume['source']).is_file()]
            check('bind-mount', not bind_errors, '; '.join(bind_errors) if bind_errors else 'Vsetky pripojene konfiguracne subory existuju.')
            ports = [(service, port) for service, config in model['services'].items()
                     for port in config.get('ports', [])]
            safe = len(ports) == 1 and ports[0][0] == 'webui' and ports[0][1].get('host_ip') == '127.0.0.1'
            check('publikovane porty', safe, 'Serverovy profil publikuje iba WebUI na loopbacku; vzdialeny pristup cez SSH.')
            ids = command(['docker', 'network', 'ls', '-q']).split()
            networks = json.loads(command(['docker', 'network', 'inspect', *ids])) if ids else []
            conflicts, own_bridges = network_conflicts(networks, model['name'])
            check('siete Docker', not conflicts, '; '.join(conflicts) if conflicts else 'Ziadna cudza siet nekoliduje s rozsahmi labu.')
        except (RuntimeError, ValueError, KeyError) as exc:
            check('Docker/Compose konfiguracia', False, str(exc))
    if 'ip' not in missing:
        try:
            conflicts = route_conflicts(json.loads(command(['ip', '-j', '-4', 'route', 'show', 'table', 'all'])), own_bridges)
            check('trasy hosta/VPN', not conflicts, '; '.join(conflicts) if conflicts else 'Rozsahy 172.18.0.0/24 a 10.45.0.0/16 nekoliduju s hostom.')
        except (RuntimeError, ValueError) as exc:
            check('trasy hosta/VPN', False, str(exc))
    disk = Path(info.get('DockerRootDir') or ROOT)
    try:
        free = shutil.disk_usage(disk).free / 1024**3
        check('volne miesto', free >= 15, str(round(free, 1)) + ' GiB; odporucame disk 80 GiB a aspon 15 GiB volnych.', True)
    except OSError:
        check('volne miesto', False, 'Neda sa overit volne miesto Docker dat.', True)
    report['status'] = 'failed' if any(c['status'] == 'failed' for c in report['checks']) else 'passed'
    os.umask(0o077)
    results = ROOT / 'results'
    results_existed = results.exists()
    results.mkdir(exist_ok=True)
    folder = None
    if args.output:
        output = args.output
    else:
        folder = Path(tempfile.mkdtemp(prefix='server-preflight-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S-'), dir=results))
        output = folder / 'summary.json'
    with output.open('x') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    if os.geteuid() == 0 and os.environ.get('SUDO_UID') and os.environ.get('SUDO_GID'):
        owner = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
        os.chown(output, *owner)
        if not results_existed:
            os.chown(results, *owner)
        if folder:
            os.chown(folder, *owner)
    print('Vysledky: ' + str(output))
    print('Kontrola prostredia nenahradza ping ani HTTP test cez UE TUN.')
    return 1 if report['status'] == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
