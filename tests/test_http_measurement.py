"""Overenie agregacie a odmietnutia nepreukazaneho prekryvu."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('http_measure', Path(__file__).resolve().parents[1] / 'scripts/measure-http.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MeasurementTests(unittest.TestCase):
    def test_percentiles_exclude_failures(self):
        rows = [dict(success=True, elapsed_ms=i) for i in range(1, 21)]
        rows.append(dict(success=False, elapsed_ms=9999))
        result = m.stats(rows)
        self.assertEqual(result['median_ms'], 10.5)
        self.assertEqual(result['p95_ms'], 19)
        self.assertEqual(result['successful'], 20)

    def test_overlap_requires_complete_request_inside_data(self):
        data = dict(start=dict(timestamp=dict(timesecs=100)),
                    intervals=[dict(sum=dict(bytes=1000, start=0, end=16))])
        m.overlap([dict(monotonic_start=103, monotonic_end=110)], data, dict(monotonic_start=100, monotonic_end=117))
        for start, end in [(100.5, 110), (103, 116.5)]:
            with self.assertRaises(m.edge.LabError):
                m.overlap([dict(monotonic_start=start, monotonic_end=end)], data, dict(monotonic_start=100, monotonic_end=117))
        data['intervals'][0]['sum']['bytes'] = 0
        with self.assertRaises(m.edge.LabError):
            m.overlap([dict(monotonic_start=103, monotonic_end=110)], data, dict(monotonic_start=100, monotonic_end=117))

    def test_empty_samples_are_not_successful(self):
        self.assertIsNone(m.stats([])['p95_ms'])
        self.assertEqual(m.stats([])['successful'], 0)

    def test_gaps_and_omitted_intervals_cannot_prove_overlap(self):
        for second in [dict(start=8, end=16, bytes=1),
                       dict(start=4, end=16, bytes=1, omitted=True),
                       dict(start=4, end=float('nan'), bytes=1)]:
            data = dict(start=dict(timestamp=dict(timesecs=100)), intervals=[
                dict(sum=dict(start=0, end=4, bytes=1)), dict(sum=second)])
            with self.assertRaises(m.edge.LabError):
                m.overlap([dict(monotonic_start=103, monotonic_end=110)], data, dict(monotonic_start=100, monotonic_end=117))

    def test_resource_summary_uses_deltas_without_double_counting_guest(self):
        memory = 'MemTotal: 4000 kB\nMemAvailable: 2000 kB\nSwapTotal: 1000 kB\nSwapFree: 900 kB\n'
        samples = [dict(cpu='cpu 10 0 10 80 0 0 0 0 5 0', memory=memory, monotonic=1),
                   dict(cpu='cpu 30 0 20 145 5 0 0 0 15 0', memory=memory, monotonic=3)]
        result = m.resource_summary(samples)
        self.assertEqual(result['cpu_busy_percent'], 30)
        self.assertEqual(result['max_swap_used_kib'], 100)
        self.assertEqual(result['seconds'], 2)

    def test_slow_control_exchange_and_impossible_timing_reject_overlap(self):
        data = dict(intervals=[dict(sum=dict(start=0, end=16, bytes=1000))])
        for end in (120, 140, 105):
            with self.subTest(end=end), self.assertRaises(m.edge.LabError):
                m.overlap([dict(monotonic_start=103, monotonic_end=110)], data,
                          dict(monotonic_start=100, monotonic_end=end))


class SeriesLifecycleTests(unittest.TestCase):
    """Simulovane zavislosti; tieto testy nie su dokazom prenosu cez TUN."""

    def run_series(self, directory, failure=None):
        root = Path(directory)
        binary = root / 'iperf3'
        binary.write_text('test fixture')
        info = dict(id='fixture', pid=os.getpid(), started_at='fixture', health='healthy',
                    ips=[m.edge.EDGE_IP, m.edge.UPF_IP])
        calls = []

        def in_ue(fd, *args, stdin=None):
            calls.append(args)
            if 'addr' in args:
                return json.dumps([dict(addr_info=[dict(family='inet', local='10.45.0.9')])])
            if 'route' in args:
                return json.dumps([dict(dev='uesimtun0')]) if 'get' in args else '[]'
            data = json.loads(stdin)
            if failure == 'interrupt':
                raise KeyboardInterrupt()
            if failure == 'http':
                raise m.edge.LabError('Simulovana chyba HTTP.')
            return json.dumps(dict(http_status=201, elapsed_ms=2,
                                   client_source_ip=data['ue_ip'], response=dict(
                                       data['reading'], status='accepted', temperature_k=296.65,
                                       observed_source_ip=m.edge.UPF_IP)))

        def trial(ue, upf, ue_ip, folder, protocol, direction, repetition, opts):
            path = folder / f'{direction}-{repetition}-client.json'
            path.write_text(json.dumps(dict(start=dict(timestamp=dict(timesecs=time.time() - 3)),
                                           intervals=[dict(sum=dict(start=0, end=16, bytes=1000))])))
            timing = folder / f'{direction}-{repetition}-timing.json'
            timing.write_text(json.dumps(dict(monotonic_start=time.monotonic() - 3,
                                               monotonic_end=time.monotonic() + 14)))
            return dict(client_json=path.name, client_timing_json=timing.name, receiver_mbps=49.9)

        # Keep the load future pending until HTTP finishes, as in a live trial.
        class Future:
            def done(self):
                return False

            def result(self):
                if failure == 'load':
                    raise m.edge.LabError('Simulovana chyba zataze.')
                return trial(*self.args)

        class Pool:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def submit(self, function, *args):
                future = Future()
                future.args = args
                return future

        with contextlib.ExitStack() as stack:
            for obj, name, value in [
                (m.edge, 'ROOT', root), (m.os, 'geteuid', lambda: 0),
                (m.iperf, 'select_iperf3', lambda _: (str(binary), 'iperf 3.21')),
                (m.edge, 'container', lambda service: dict(info, ips=[m.edge.EDGE_IP] if service == 'edge' else info['ips'])),
                (m.edge, 'command', lambda *a: 'fixture version'), (m.edge, 'dc', lambda *a: 'fixture version'),
                (m.edge, 'in_ue', in_ue), (m.time, 'sleep', lambda _: None),
                (m.concurrent.futures, 'ThreadPoolExecutor', Pool)]:
                stack.enter_context(patch.object(obj, name, value))
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            stack.enter_context(patch('sys.argv', ['measure-http.py', '--repetitions', '1', '--requests', '2']))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = m.main()
        folder, = (root / 'results').glob('http-*')
        summary = json.loads((folder / 'summary.json').read_text())
        self.assertTrue((folder / 'report.md').is_file())
        self.assertTrue((folder / 'requests.csv').is_file())
        self.assertTrue(any('del' in args for args in calls))
        return code, summary, folder

    def test_success_exports_all_conditions_and_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            code, summary, folder = self.run_series(directory)
            self.assertEqual(code, 0)
            self.assertEqual(summary['status'], 'complete')
            self.assertEqual(summary['http']['successful'], 6)
            self.assertEqual(set(summary['by_condition']), {'idle', 'UL', 'DL'})
            self.assertTrue(all(g['status'] == 'complete' for g in summary['groups']))
            self.assertIn('True', (folder / 'requests.csv').read_text())

    def test_http_failure_load_failure_and_interrupt_preserve_partial_results(self):
        for failure in ('http', 'load', 'interrupt'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                code, summary, folder = self.run_series(directory, failure)
                self.assertEqual(code, 1)
                self.assertEqual(summary['status'], 'failed')
                self.assertGreater(summary['http']['requests'], 0)
                self.assertEqual(summary['groups'][-1]['status'], 'failed')
                self.assertTrue((folder / 'requests.jsonl').read_text().strip())
