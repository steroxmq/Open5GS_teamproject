"""Obnova po SIGTERM a zrusenie vlastnych procesov bez Dockeru."""
import concurrent.futures
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(filename):
    spec = importlib.util.spec_from_file_location(filename, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


edge = load('check-edge.py')
iperf = load('measure-compose.py')


class PilotCleanupTests(unittest.TestCase):
    def test_sigterm_saves_failed_report_and_removes_only_created_route(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                calls = []
                info = dict(id='fixture', pid=os.getpid(), started_at='fixture', health='healthy',
                            ips=[edge.EDGE_IP])

                def in_ue(fd, *args, stdin=None):
                    calls.append(args)
                    if 'addr' in args:
                        return json.dumps([dict(addr_info=[dict(family='inet', local='10.45.0.9')])])
                    if 'show' in args:
                        return json.dumps([dict(dev='uesimtun0', prefsrc='10.45.0.9')] if existing else [])
                    if 'get' in args:
                        return json.dumps([dict(dev='uesimtun0')])
                    if 'route' in args:
                        return ''
                    os.kill(os.getpid(), signal.SIGTERM)

                previous = signal.getsignal(signal.SIGTERM)
                real_open = os.open

                def fixture_open(path, *args, **kwargs):
                    # Namespace je simulovany; test nepotrebuje pristup do /proc/PID/ns.
                    if str(path).startswith('/proc/') and str(path).endswith('/ns/net'):
                        path = os.devnull
                    return real_open(path, *args, **kwargs)

                with patch.object(edge, 'ROOT', Path(directory)), patch.object(edge.os, 'geteuid', return_value=0), \
                        patch.object(edge.os, 'open', side_effect=fixture_open), \
                        patch.object(edge.shutil, 'which', return_value='/fixture/tool'), \
                        patch.object(edge, 'container', side_effect=lambda service: dict(info, ips=[edge.UPF_IP] if service == 'upf' else info['ips'])), \
                        patch.object(edge, 'in_ue', side_effect=in_ue), patch.object(edge, 'dc', return_value=''), \
                        patch.dict(os.environ, {}, clear=True), patch('sys.argv', ['check-edge.py']), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    (Path(directory) / 'compose.edge.yaml').touch()
                    code = edge.main()
                self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
                self.assertEqual(code, 130)
                folder, = (Path(directory) / 'results').glob('edge-*')
                summary = json.loads((folder / 'summary.json').read_text())
                self.assertEqual(summary['status'], 'failed')
                self.assertEqual(any('del' in args for args in calls), not existing)
                self.assertTrue((folder / 'report.md').is_file())


class ProcessCleanupTests(unittest.TestCase):
    def test_cancel_reaps_client_and_its_child_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / 'child.py'
            child.write_text("import signal, sys, time\nfrom pathlib import Path\n"
                             "def stop(*args):\n    Path(sys.argv[1]).write_text('stopped')\n    sys.exit(0)\n"
                             "signal.signal(signal.SIGTERM, stop)\nprint('ready', flush=True)\ntime.sleep(30)\n")
            parent = root / 'parent.py'
            parent.write_text("import json, os, signal, subprocess, sys, time\nfrom pathlib import Path\n"
                              "p = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], stdout=subprocess.PIPE, text=True)\n"
                              "def stop(*args):\n    p.wait(timeout=2)\n    sys.exit(0)\n"
                              "signal.signal(signal.SIGTERM, stop)\np.stdout.readline()\n"
                              "Path(sys.argv[3]).write_text(json.dumps([os.getpid(), p.pid]))\ntime.sleep(30)\n")
            ready, stopped = root / 'ready.json', root / 'stopped'
            cancel = threading.Event()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(iperf.command, [sys.executable, str(parent), str(child), str(stopped), str(ready)],
                                     timeout=10, cancel_event=cancel)
                deadline = time.monotonic() + 5
                try:
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(ready.exists())
                finally:
                    cancel.set()
                with self.assertRaises(iperf.LabError):
                    future.result(timeout=5)
            self.assertTrue(stopped.exists())
            for pid in json.loads(ready.read_text()):
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    def test_timeout_reaps_process(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / 'pid'
            code = "import os, sys, time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
            with self.assertRaises(iperf.LabError):
                iperf.command([sys.executable, '-c', code, str(pidfile)], timeout=.5)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)
