"""Overenie agregacie a odmietnutia nepreukazaneho prekryvu."""
import importlib.util
from pathlib import Path
import unittest

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
                    intervals=[dict(sum=dict(bytes=1000, end=16))])
        m.overlap([dict(epoch_start=103, epoch_end=110)], data)
        for start, end in [(100.5, 110), (103, 115.5)]:
            with self.assertRaises(m.edge.LabError):
                m.overlap([dict(epoch_start=start, epoch_end=end)], data)
        data['intervals'][0]['sum']['bytes'] = 0
        with self.assertRaises(m.edge.LabError):
            m.overlap([dict(epoch_start=103, epoch_end=110)], data)

    def test_empty_samples_are_not_successful(self):
        self.assertIsNone(m.stats([])['p95_ms'])
        self.assertEqual(m.stats([])['successful'], 0)
