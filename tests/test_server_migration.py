"""Kontroly adresneho planu a exportu; simulovany Docker nie je ziva zaloha."""
import fcntl
import gzip
import importlib.util
import json
import os
import re
import shlex
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('preflight', ROOT / 'scripts/server-preflight.py')
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def docker_network(name, cidr, own=False):
    return dict(Name=name, Id='a' * 64, Driver='bridge',
                IPAM={'Config': [{'Subnet': cidr}]},
                Labels={'com.docker.compose.project': 'open5gs-ueransim',
                        'com.docker.compose.network': 'core'} if own else {})


class PreflightTests(unittest.TestCase):
    def test_foreign_wider_docker_network_is_rejected(self):
        conflicts, bridges = preflight.network_conflicts(
            [docker_network('foreign', '172.18.0.0/16')], 'open5gs-ueransim')
        self.assertTrue(conflicts)
        self.assertFalse(bridges)

    def test_own_core_allowed_but_foreign_ue_subnet_rejected(self):
        conflicts, bridges = preflight.network_conflicts(
            [docker_network('own', '172.18.0.0/24', True),
             docker_network('other', '10.45.1.0/24')], 'open5gs-ueransim')
        self.assertEqual(bridges, {'br-' + 'a' * 12})
        self.assertEqual(conflicts, ['other: 10.45.1.0/24'])

    def test_labels_do_not_hide_wrong_own_subnet(self):
        conflicts, bridges = preflight.network_conflicts(
            [docker_network('wrong', '172.18.0.0/16', True)], 'open5gs-ueransim')
        self.assertTrue(conflicts)
        self.assertFalse(bridges)

    def test_default_and_own_bridge_routes_allowed(self):
        routes = [dict(dst='default', dev='ens18'),
                  dict(dst='172.18.0.0/24', dev='br-own'),
                  dict(dst='172.18.0.1', dev='br-own', type='local'),
                  dict(dst='192.168.10.0/24', dev='ens18')]
        self.assertEqual(preflight.route_conflicts(routes, {'br-own'}), [])

    def test_vpn_native_ogstun_and_other_bridge_are_conflicts(self):
        routes = [dict(dst='10.0.0.0/8', dev='vpn0'),
                  dict(dst='10.45.0.0/16', dev='ogstun'),
                  dict(dst='172.18.0.0/24', dev='br-other')]
        self.assertEqual(len(preflight.route_conflicts(routes, {'br-own'})), 3)

    def test_non_lab_and_ipv6_do_not_conflict(self):
        self.assertFalse(preflight.overlaps('172.17.0.0/16'))
        self.assertFalse(preflight.overlaps('2001:db8::/32'))

    def test_env_validation_reports_names_not_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            secret = 'invalid-private-fixture'
            path.write_text('UE_KEY=' + secret + '\nUE_OPC=' + 'A' * 32 + '\n')
            path.chmod(0o600)
            errors = preflight.env_errors(path)
            self.assertIn('Neplatny format UE_KEY', errors)
            self.assertNotIn(secret, str(errors))

    def test_env_permissions_duplicates_and_public_binding_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('UE_KEY=' + 'A' * 32 + '\nUE_KEY=' + 'B' * 32 +
                            '\nUE_OPC=' + 'C' * 32 + '\nWEBUI_BIND_IP=0.0.0.0\n')
            path.chmod(0o644)
            self.assertEqual(len(preflight.env_errors(path)), 3)

    def test_private_quoted_keys_and_default_binding_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('UE_KEY="' + 'A' * 32 + '"\nUE_OPC=\'' + 'B' * 32 + "'\n")
            path.chmod(0o600)
            self.assertEqual(preflight.env_errors(path), [])


FAKE_DOCKER = r'''#!/usr/bin/env python3
import gzip, json, os, sys
args = sys.argv[1:]
if os.environ.get('FIXTURE_CALLS'):
    with open(os.environ['FIXTURE_CALLS'], 'a') as log:
        log.write(json.dumps(args) + '\n')
if 'inspect' in args:
    print('sha256:changed' if 'image' in args and os.environ.get('FIXTURE_RETAG') else 'sha256:fixture')
elif 'ps' in args:
    print(os.environ.get('FIXTURE_RUNNING', 'mongo') if '--services' in args else 'fixture-mongo')
elif 'mongosh' in args:
    print(json.dumps([{'collection':'accounts','count':1},{'collection':'subscribers','count':1}]))
elif 'mongodump' in args and '--version' in args:
    print('mongodump version: fixture')
elif 'mongodump' in args:
    if os.environ.get('FIXTURE_DUMP') == 'failed':
        sys.stdout.buffer.write(b'partial-fixture')
        sys.exit(2)
    if os.environ.get('FIXTURE_DUMP') == 'corrupt':
        sys.stdout.buffer.write(b'not-a-gzip-archive')
    else:
        sys.stdout.buffer.write(gzip.compress(b'simulated archive, not MongoDB data'))
else:
    sys.exit(3)
'''


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        shutil.copyfile(ROOT / 'scripts/backup-mongo.sh', self.root / 'scripts/backup-mongo.sh')
        (self.root / '.env').write_text('UE_KEY=private-fixture\n')
        (self.root / 'compose.yaml').write_text('fixture\n')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.bin / 'docker').write_text(FAKE_DOCKER)
        (self.bin / 'docker').chmod(0o755)
        (self.bin / 'sudo').write_text('#!/usr/bin/env python3\nimport os,sys\n'
                                     'if sys.argv[1] == "-v": sys.exit(0)\n'
                                     'args = sys.argv[1:]\n'
                                     'if args[0] == "-n": args = args[1:]\n'
                                     'os.execvp(args[0], args)\n')
        (self.bin / 'sudo').chmod(0o755)
        self.env = os.environ | {'PATH': str(self.bin) + os.pathsep + os.environ['PATH']}
        self.git('init', '-q')
        self.git('add', 'scripts/backup-mongo.sh', 'compose.yaml')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'fixture')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)

    def run_backup(self, **env):
        return subprocess.run(['bash', str(self.root / 'scripts/backup-mongo.sh')],
                              env=self.env | env, text=True, capture_output=True, timeout=15)

    def test_success_is_private_compressed_and_checksummed(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        folder, = list((self.root / 'backups').iterdir())
        self.assertEqual(gzip.decompress((folder / 'open5gs.archive.gz').read_bytes()),
                         b'simulated archive, not MongoDB data')
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((folder / 'open5gs.archive.gz').stat().st_mode), 0o600)
        subprocess.run(['sha256sum', '--check', 'SHA256SUMS'], cwd=folder, check=True, capture_output=True)
        self.assertFalse((folder / 'lab.env').exists())

    def test_running_writer_refuses_before_creating_backup(self):
        for writer in ('webui', 'edge'):
            with self.subTest(writer=writer):
                result = self.run_backup(FIXTURE_RUNNING='mongo\n' + writer)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / 'backups').exists())

    def test_moved_mongo_tag_refuses_before_creating_backup(self):
        result = self.run_backup(FIXTURE_RETAG='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'backups').exists())

    def test_failed_export_removes_partial_and_keeps_original_env(self):
        result = self.run_backup(FIXTURE_DUMP='failed')
        self.assertNotEqual(result.returncode, 0)
        folder, = list((self.root / 'backups').iterdir())
        self.assertFalse((folder / 'open5gs.archive.gz').exists())
        self.assertFalse((folder / 'open5gs.archive.gz.partial').exists())
        self.assertEqual((self.root / '.env').read_text(), 'UE_KEY=private-fixture\n')

    def test_invalid_gzip_not_promoted_to_archive(self):
        result = self.run_backup(FIXTURE_DUMP='corrupt')
        self.assertNotEqual(result.returncode, 0)
        folder, = list((self.root / 'backups').iterdir())
        self.assertFalse((folder / 'open5gs.archive.gz').exists())

    def test_shared_lock_refuses_concurrent_backup(self):
        (self.root / 'results').mkdir()
        with (self.root / 'results/.compose-up.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'backups').exists())

    def test_uncommitted_tracked_changes_refuse_backup(self):
        (self.root / 'compose.yaml').write_text('modified fixture\n')
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'backups').exists())

    def preparation_block(self):
        blocks = re.findall(r'```bash\n(.*?)```', (ROOT / 'docs/server-migration.md').read_text(), re.S)
        block, = [b for b in blocks if 'git status -sb' in b]
        return block.replace('cd ~/open5gs-ueransim-lab', 'cd -- ' + shlex.quote(str(self.root)))

    def test_documented_preparation_does_not_stop_when_lock_is_busy(self):
        (self.root / 'results').mkdir()
        calls = self.root / 'docker-calls.jsonl'
        with (self.root / 'results/.compose-up.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(['bash', '-c', self.preparation_block()],
                                    env=self.env | {'FIXTURE_CALLS': str(calls)},
                                    text=True, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(calls.exists())

    def test_documented_preparation_stops_after_failed_stop(self):
        calls = self.root / 'docker-calls.jsonl'
        result = subprocess.run(['bash', '-c', self.preparation_block()],
                                env=self.env | {'FIXTURE_CALLS': str(calls)},
                                text=True, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        records = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertIn('stop', records[0])
        self.assertFalse((self.root / 'backups').exists())


if __name__ == '__main__':
    unittest.main()
