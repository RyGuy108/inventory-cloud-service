#!/usr/bin/env python3
"""Offline tests of the release policy; no scanner, Docker, network, or AWS required."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name('check_security_reports.py').resolve()


class ReleaseSecurityTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.directory = Path(self.workspace.name)
        self.jar = self.directory / 'application.jar'
        self.jar.write_bytes(b'packaged application fixture')
        self.image_id = 'sha256:' + 'a' * 64
        (self.directory / 'image-id.json').write_text(json.dumps(self.image_id))
        self.report('dependencies')
        self.report('image')

    def report(self, name, vulnerabilities=None, packages=None):
        data = {
            'Results': [{
                'Type': 'jar',
                'Packages': [{'Name': 'example', 'Version': '1.0'}] if packages is None else packages,
                'Vulnerabilities': vulnerabilities or [],
            }],
            'Metadata': {'ImageConfig': {'os': 'linux', 'architecture': 'amd64'}},
        }
        (self.directory / f'{name}.json').write_text(json.dumps(data))

    def evaluate(self, scanner_failed=False):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.directory), str(self.jar),
             'inventory:verified', '1' if scanner_failed else '0'],
            text=True, capture_output=True,
        )
        summary = json.loads((self.directory / 'summary.json').read_text())
        return result.returncode, summary

    def test_clean_inventory_passes_and_records_artifact_identity(self):
        code, summary = self.evaluate()
        self.assertEqual(0, code)
        self.assertTrue(summary['passed'])
        self.assertEqual(self.image_id, summary['image_id'])
        self.assertEqual(hashlib.sha256(self.jar.read_bytes()).hexdigest(), summary['jar_sha256'])
        self.assertEqual('linux/amd64', summary['image_platform'])

    def test_unfixed_high_dependency_blocks_release(self):
        self.report('dependencies', [{'Severity': 'HIGH', 'FixedVersion': ''}])
        code, summary = self.evaluate()
        self.assertEqual(1, code)
        self.assertFalse(summary['passed'])
        self.assertFalse(summary['ignore_unfixed'])

    def test_critical_image_finding_blocks_release(self):
        self.report('image', [{'Severity': 'CRITICAL', 'FixedVersion': '2.0'}])
        code, summary = self.evaluate()
        self.assertEqual(1, code)
        self.assertFalse(summary['passed'])

    def test_nonblocking_findings_remain_visible(self):
        self.report('image', [{'Severity': 'MEDIUM'}, {'Severity': 'LOW'}, {'Severity': 'MEDIUM'}])
        code, summary = self.evaluate()
        self.assertEqual(0, code)
        self.assertEqual({'MEDIUM': 2, 'LOW': 1}, summary['reports']['image']['vulnerabilities_by_severity'])

    def test_failed_scanner_blocks_even_if_reports_look_clean(self):
        code, summary = self.evaluate(scanner_failed=True)
        self.assertEqual(1, code)
        self.assertTrue(summary['scan_errors'])

    def test_missing_report_blocks_release(self):
        (self.directory / 'image.json').unlink()
        code, summary = self.evaluate()
        self.assertEqual(1, code)
        self.assertTrue(summary['scan_errors'])

    def test_invalid_json_blocks_release(self):
        (self.directory / 'dependencies.json').write_text('{truncated')
        code, summary = self.evaluate()
        self.assertEqual(1, code)
        self.assertTrue(summary['scan_errors'])

    def test_empty_dependency_inventory_blocks_release(self):
        self.report('dependencies', packages=[])
        code, summary = self.evaluate()
        self.assertEqual(1, code)
        self.assertTrue(summary['scan_errors'])

    def test_missing_results_blocks_release(self):
        (self.directory / 'image.json').write_text('{}')
        code, summary = self.evaluate()
        self.assertEqual(1, code)
        self.assertTrue(summary['scan_errors'])


if __name__ == '__main__':
    unittest.main()
