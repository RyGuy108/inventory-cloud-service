#!/usr/bin/env python3
"""Read-only preflight safeguards with synthetic metadata; never contacts AWS."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('cloud_preflight', Path(__file__).with_name('cloud-preflight.py'))
PREFLIGHT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREFLIGHT)
CONFIG = {
    'aws_region': 'us-east-1', 'aws_account_id': '123456789012',
    'state_bucket_name': 'inventory-state-123456789012',
    'container_image': '123456789012.dkr.ecr.us-east-1.amazonaws.com/inventory@sha256:' + 'a' * 64,
    'certificate_arn': 'arn:aws:acm:us-east-1:123456789012:certificate/12345678-1234-1234-1234-123456789012',
    'public_hostname': 'inventory.acme.test', 'jwt_issuer_uri': 'https://identity.acme.test/tenant',
    'jwt_audience': 'inventory-api',
    'runtime_database_secret_arn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:runtime-AbC123',
    'github_repository': 'owner/inventory',
    'github_oidc_provider_arn': 'arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com',
}


class FakeMetadata:
    def __init__(self):
        self.calls = []
        self.data = {
            ('sts', 'get-caller-identity'): {'Account': '123456789012'},
            ('ecr', 'describe-repositories'): {'repositories': [{'imageTagMutability': 'IMMUTABLE'}]},
            ('ecr', 'describe-images'): {'imageDetails': [{'imageDigest': 'sha256:' + 'a' * 64}]},
            ('acm', 'describe-certificate'): {'Certificate': {'Status': 'ISSUED', 'NotAfter': '2099-01-01T00:00:00+00:00', 'SubjectAlternativeNames': ['*.acme.test']}},
            ('secretsmanager', 'describe-secret'): {'VersionIdsToStages': {'version-1': ['AWSCURRENT']}},
            ('s3api', 'get-bucket-versioning'): {'Status': 'Enabled'},
            ('s3api', 'get-public-access-block'): {'PublicAccessBlockConfiguration': {k: True for k in ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')}},
            ('s3api', 'get-bucket-encryption'): {'ServerSideEncryptionConfiguration': {'Rules': [{'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]}},
            ('ec2', 'describe-availability-zones'): {'AvailabilityZones': [{}, {}]},
            ('iam', 'get-open-id-connect-provider'): {'Url': 'token.actions.githubusercontent.com', 'ClientIDList': ['sts.amazonaws.com']},
        }

    def read(self, service, operation, *args):
        self.calls.append((service, operation))
        self.assert_allowed(service, operation)
        return self.data[(service, operation)]

    def assert_allowed(self, service, operation):
        assert (service, operation) in PREFLIGHT.READ_COMMANDS


class CloudPreflightTest(unittest.TestCase):
    def test_valid_offline_identifiers(self):
        self.assertTrue(all(c['status'] == 'pass' for c in PREFLIGHT.validate_config(CONFIG)))

    def test_missing_settings_fail_without_revealing_values(self):
        checks = PREFLIGHT.validate_config({})
        self.assertEqual(set(PREFLIGHT.REQUIRED), {c['check'] for c in checks})
        self.assertTrue(all(c['status'] == 'fail' for c in checks))

    def test_cross_account_or_region_resources_are_rejected(self):
        for field in ('container_image', 'certificate_arn', 'runtime_database_secret_arn'):
            for original, replacement in [('123456789012', '999999999999'), ('us-east-1', 'us-west-2')]:
                with self.subTest(field=field, replacement=replacement):
                    config = {**CONFIG, field: CONFIG[field].replace(original, replacement)}
                    self.assertEqual('fail', next(c for c in PREFLIGHT.validate_config(config) if c['check'] == field)['status'])

    def test_secret_fields_rejected_and_values_never_echoed(self):
        checks = PREFLIGHT.validate_config({**CONFIG, 'password': 'TOP_SECRET_SENTINEL'})
        self.assertTrue(any(c['status'] == 'fail' for c in checks))
        self.assertNotIn('TOP_SECRET_SENTINEL', json.dumps(checks))

    def test_issuer_rejects_credentials_query_fragment_and_http(self):
        for issuer in ('https://user:secret@id.acme.test', 'https://id.acme.test/?token=secret', 'https://id.acme.test/#secret', 'http://id.acme.test'):
            with self.subTest(issuer=issuer):
                checks = PREFLIGHT.validate_config({**CONFIG, 'jwt_issuer_uri': issuer})
                self.assertEqual('fail', next(c for c in checks if c['check'] == 'jwt_issuer_uri')['status'])
                self.assertNotIn(issuer, json.dumps(checks))

    def test_aws_metadata_success_is_allowlisted_and_never_gets_secret_value(self):
        reader = FakeMetadata()
        checks = PREFLIGHT.validate_aws(CONFIG, reader)
        self.assertTrue(all(c['status'] == 'pass' for c in checks), checks)
        self.assertNotIn(('secretsmanager', 'get-secret-value'), reader.calls)
        self.assertEqual(1, reader.calls.count(('s3api', 'get-public-access-block')))

    def test_wrong_active_account_stops_further_metadata_reads(self):
        reader = FakeMetadata()
        reader.data[('sts', 'get-caller-identity')]['Account'] = '999999999999'
        checks = PREFLIGHT.validate_aws(CONFIG, reader)
        self.assertEqual('fail', checks[0]['status'])
        self.assertEqual([('sts', 'get-caller-identity')], reader.calls)

    def test_custom_kms_key_blocks_unsupported_secret(self):
        reader = FakeMetadata()
        reader.data[('secretsmanager', 'describe-secret')]['KmsKeyId'] = 'custom-key'
        checks = PREFLIGHT.validate_aws(CONFIG, reader)
        self.assertEqual('fail', next(c for c in checks if c['check'] == 'runtime_secret_metadata')['status'])

    def test_missing_current_secret_version_blocks_release(self):
        reader = FakeMetadata()
        reader.data[('secretsmanager', 'describe-secret')] = {'VersionIdsToStages': {'v': ['AWSPREVIOUS']}}
        checks = PREFLIGHT.validate_aws(CONFIG, reader)
        self.assertEqual('fail', next(c for c in checks if c['check'] == 'runtime_secret_metadata')['status'])

    def test_secret_pending_deletion_blocks_release(self):
        reader = FakeMetadata()
        reader.data[('secretsmanager', 'describe-secret')]['DeletedDate'] = '2026-10-01T00:00:00Z'
        checks = PREFLIGHT.validate_aws(CONFIG, reader)
        self.assertEqual('fail', next(c for c in checks if c['check'] == 'runtime_secret_metadata')['status'])

    def test_wildcard_certificate_does_not_cover_multiple_labels(self):
        reader = FakeMetadata()
        checks = PREFLIGHT.validate_aws({**CONFIG, 'public_hostname': 'nested.inventory.acme.test'}, reader)
        self.assertEqual('fail', next(c for c in checks if c['check'] == 'acm_certificate_ready')['status'])

    def test_mutating_command_is_rejected_before_subprocess(self):
        reader = PREFLIGHT.AwsMetadata('us-east-1')
        with patch.object(PREFLIGHT.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                reader.read('ecs', 'update-service', '--service', 'inventory')
            with self.assertRaises(ValueError):
                reader.read('secretsmanager', 'get-secret-value', '--secret-id', 'runtime')
            run.assert_not_called()

    def test_offline_cli_never_invokes_aws_and_never_claims_deployment_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config = directory / 'config.json'
            config.write_text(json.dumps(CONFIG))
            output = directory / 'report.json'
            with patch.object(PREFLIGHT.subprocess, 'run') as run, patch('builtins.print'):
                code = PREFLIGHT.main(['--config', str(config), '--output', str(output)])
            run.assert_not_called()
            report = json.loads(output.read_text())
            self.assertEqual(0, code)
            self.assertFalse(report['aws_metadata_checked'])
            self.assertFalse(report['deployment_ready'])
            self.assertEqual('configuration_valid_unverified', report['readiness'])

    def test_invalid_configuration_prevents_requested_aws_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(PREFLIGHT.subprocess, 'run') as run, patch('builtins.print'):
                code = PREFLIGHT.main(['--aws-read-only', '--output', str(Path(temporary) / 'report.json')])
            run.assert_not_called()
            self.assertEqual(1, code)


if __name__ == '__main__':
    unittest.main()
