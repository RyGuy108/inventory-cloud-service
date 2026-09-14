#!/usr/bin/env python3
"""Validate non-secret deployment identifiers; AWS access is opt-in and metadata-only."""
import argparse
import datetime
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlparse

REQUIRED = (
    'aws_region', 'aws_account_id', 'state_bucket_name', 'container_image',
    'certificate_arn', 'public_hostname', 'jwt_issuer_uri', 'jwt_audience',
    'runtime_database_secret_arn',
)
OPTIONAL = ('github_repository', 'github_oidc_provider_arn', 'alarm_actions')
# Deliberately excludes every mutating command and GetSecretValue.
READ_COMMANDS = {
    ('sts', 'get-caller-identity'), ('ecr', 'describe-repositories'),
    ('ecr', 'describe-images'), ('acm', 'describe-certificate'),
    ('secretsmanager', 'describe-secret'), ('iam', 'get-open-id-connect-provider'),
    ('s3api', 'get-bucket-versioning'), ('s3api', 'get-public-access-block'),
    ('s3api', 'get-bucket-encryption'), ('ec2', 'describe-availability-zones'),
}
IMAGE_PATTERN = re.compile(r'^(\d{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/([a-z0-9]+(?:[._/-][a-z0-9]+)*)@(sha256:[a-f0-9]{64})$')
REGION_PATTERN = re.compile(r'^(?:us|eu|ap|ca|sa|me|af|il|mx)-[a-z]+-\d+$')
HOST_PATTERN = re.compile(r'^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$')


def result(name, passed, detail):
    return {'check': name, 'status': 'pass' if passed else 'fail', 'detail': detail}


def nonsecret_https(value):
    try:
        parsed = urlparse(value)
        return (parsed.scheme == 'https' and bool(parsed.hostname) and
                parsed.username is None and parsed.password is None and
                not parsed.query and not parsed.fragment and parsed.port in (None, 443))
    except (ValueError, TypeError):
        return False


def validate_config(config):
    checks = []
    if not isinstance(config, dict):
        return [result('configuration', False, 'Configuration must be a JSON object of non-secret identifiers.')]
    if set(config) - set(REQUIRED + OPTIONAL):
        checks.append(result('configuration_fields', False, 'Unsupported fields supplied. Supply only documented non-secret identifiers; never credentials or passwords.'))
    for field in REQUIRED:
        value = config.get(field)
        if not isinstance(value, str) or not value.strip():
            checks.append(result(field, False, 'Missing required non-secret deployment setting.'))
    if any(c['status'] == 'fail' for c in checks):
        return checks
    region, account = config['aws_region'], config['aws_account_id']
    checks.append(result('aws_region', bool(REGION_PATTERN.fullmatch(region)), 'Use a commercial AWS region supported by this stack.'))
    checks.append(result('aws_account_id', bool(re.fullmatch(r'\d{12}', account)), 'An explicit 12-digit expected account ID prevents using the wrong AWS session.'))
    bucket = config['state_bucket_name']
    bucket_valid = (3 <= len(bucket) <= 63 and re.fullmatch(r'[a-z0-9][a-z0-9.-]+[a-z0-9]', bucket)
                    and '..' not in bucket and not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', bucket))
    checks.append(result('state_bucket_name', bool(bucket_valid), 'Supply the existing bootstrap state bucket name.'))
    image = IMAGE_PATTERN.fullmatch(config['container_image'])
    checks.append(result('container_image', bool(image and image[1] == account and image[2] == region), 'Use a digest-pinned ECR image in the expected account and region.'))
    cert_prefix = f'arn:aws:acm:{region}:{account}:certificate/'
    cert = config['certificate_arn']
    checks.append(result('certificate_arn', cert.startswith(cert_prefix) and bool(re.fullmatch(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', cert.removeprefix(cert_prefix))), 'Use an ACM certificate ARN from the expected account and region.'))
    hostname = config['public_hostname']
    checks.append(result('public_hostname', bool(HOST_PATTERN.fullmatch(hostname)) and not hostname.endswith('.example.com'), 'Use the real public DNS hostname; example.com placeholders are not deployment-ready.'))
    issuer = config['jwt_issuer_uri']
    checks.append(result('jwt_issuer_uri', nonsecret_https(issuer) and not (urlparse(issuer).hostname or '').endswith('example.com'), 'Use the real HTTPS OIDC issuer without credentials, query strings, or fragments.'))
    checks.append(result('jwt_audience', bool(config['jwt_audience'].strip()) and not any(c.isspace() for c in config['jwt_audience']), 'Supply the audience configured in the identity provider.'))
    secret_prefix = f'arn:aws:secretsmanager:{region}:{account}:secret:'
    secret = config['runtime_database_secret_arn']
    checks.append(result('runtime_database_secret_arn', secret.startswith(secret_prefix) and bool(re.fullmatch(r'[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}', secret.removeprefix(secret_prefix))), 'Use the complete runtime secret ARN in the expected account and region; never a secret value.'))
    github = config.get('github_repository')
    oidc = config.get('github_oidc_provider_arn')
    if github is not None or oidc is not None:
        checks.append(result('github_repository', isinstance(github, str) and bool(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', github)), 'Supply owner/repository for the optional GitHub release roles.'))
        checks.append(result('github_oidc_provider_arn', oidc == f'arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com', 'The GitHub OIDC provider must belong to the expected account.'))
    alarms = config.get('alarm_actions', [])
    checks.append(result('alarm_actions', isinstance(alarms, list) and all(isinstance(v, str) and v.startswith(f'arn:aws:sns:{region}:{account}:') for v in alarms), 'Alarm destinations, if supplied, must be SNS topic ARNs in the expected account and region.'))
    return checks


class AwsMetadata:
    def __init__(self, region):
        self.region = region

    def read(self, service, operation, *arguments):
        if (service, operation) not in READ_COMMANDS:
            raise ValueError('Command is outside the read-only allowlist.')
        # No shell interpolation and no raw AWS response/error output. Metadata only.
        command = ['aws', service, operation, *arguments, '--region', self.region,
                   '--output', 'json', '--no-cli-pager', '--cli-connect-timeout', '5', '--cli-read-timeout', '15']
        run = subprocess.run(command, text=True, capture_output=True, timeout=45,
                             env={**os.environ, 'AWS_PAGER': '', 'AWS_EC2_METADATA_DISABLED': 'true',
                                  'AWS_IGNORE_CONFIGURED_ENDPOINT_URLS': 'true'})
        if run.returncode:
            raise RuntimeError('AWS metadata request failed; verify existence and read permissions. Raw errors are omitted.')
        return json.loads(run.stdout)


def validate_aws(config, reader):
    checks = []

    def check(name, callback, detail):
        try:
            passed = bool(callback())
            checks.append(result(name, passed, detail))
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, KeyError, TypeError, IndexError):
            checks.append(result(name, False, 'Metadata could not be verified. Check the prerequisite and read permissions; no secret values were requested.'))

    check('aws_account_access', lambda: reader.read('sts', 'get-caller-identity')['Account'] == config['aws_account_id'], 'The active AWS session must match the explicitly expected account.')
    # An unexpected account is a stop condition, not an invitation to inspect it further.
    if checks[-1]['status'] == 'fail':
        return checks
    image = IMAGE_PATTERN.fullmatch(config['container_image'])
    repository, digest = image[3], image[4]
    check('ecr_immutable_repository', lambda: reader.read('ecr', 'describe-repositories', '--repository-names', repository)['repositories'][0]['imageTagMutability'] == 'IMMUTABLE', 'The destination ECR repository must use immutable tags.')
    check('ecr_image_exists', lambda: any(d.get('imageDigest') == digest for d in reader.read('ecr', 'describe-images', '--repository-name', repository, '--image-ids', f'imageDigest={digest}').get('imageDetails', [])), 'The supplied image digest must already exist in ECR.')

    def certificate_ready():
        certificate = reader.read('acm', 'describe-certificate', '--certificate-arn', config['certificate_arn'])['Certificate']
        hostname = config['public_hostname'].lower()
        names = certificate.get('SubjectAlternativeNames', [])
        # ACM wildcards match exactly one DNS label, never multiple levels.
        matches = any(hostname == n.lower() or (n.startswith('*.') and hostname.count('.') == n.count('.') and fnmatch.fnmatchcase(hostname, n.lower())) for n in names)
        expires = certificate.get('NotAfter', 0)
        if isinstance(expires, str):
            expires = datetime.datetime.fromisoformat(expires.replace('Z', '+00:00')).timestamp()
        return certificate.get('Status') == 'ISSUED' and expires > datetime.datetime.now(datetime.timezone.utc).timestamp() and matches

    check('acm_certificate_ready', certificate_ready, 'The certificate must be issued, unexpired, and cover the public hostname.')

    def secret_ready():
        secret = reader.read('secretsmanager', 'describe-secret', '--secret-id', config['runtime_database_secret_arn'])
        current = any('AWSCURRENT' in stages for stages in secret.get('VersionIdsToStages', {}).values())
        return not secret.get('DeletedDate') and secret.get('KmsKeyId') in (None, '', 'alias/aws/secretsmanager') and current

    check('runtime_secret_metadata', secret_ready, 'The runtime secret must have a current version, no pending deletion, and the default Secrets Manager key. Its password content is intentionally not read.')
    bucket = config['state_bucket_name']
    check('state_bucket_versioning', lambda: reader.read('s3api', 'get-bucket-versioning', '--bucket', bucket, '--expected-bucket-owner', config['aws_account_id']).get('Status') == 'Enabled', 'Terraform state versioning must be enabled.')
    def bucket_private():
        protection = reader.read('s3api', 'get-public-access-block', '--bucket', bucket, '--expected-bucket-owner', config['aws_account_id'])['PublicAccessBlockConfiguration']
        return all(protection.get(k) is True for k in ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets'))
    check('state_bucket_public_access', bucket_private, 'All four S3 public-access protections must be enabled.')
    check('state_bucket_encryption', lambda: bool(reader.read('s3api', 'get-bucket-encryption', '--bucket', bucket, '--expected-bucket-owner', config['aws_account_id'])['ServerSideEncryptionConfiguration']['Rules']), 'State bucket default encryption must be configured.')
    check('two_availability_zones', lambda: len(reader.read('ec2', 'describe-availability-zones', '--filters', 'Name=state,Values=available')['AvailabilityZones']) >= 2, 'At least two available zones are required for the application and database subnets.')
    if config.get('github_oidc_provider_arn'):
        def provider_ready():
            provider = reader.read('iam', 'get-open-id-connect-provider', '--open-id-connect-provider-arn', config['github_oidc_provider_arn'])
            return provider.get('Url', '').removeprefix('https://') == 'token.actions.githubusercontent.com' and 'sts.amazonaws.com' in provider.get('ClientIDList', [])
        check('github_oidc_provider', provider_ready, 'The GitHub OIDC provider must have the expected URL and STS audience.')
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='JSON file containing non-secret identifiers only; no implicit environment/config discovery')
    parser.add_argument('--aws-read-only', action='store_true', help='Explicitly allow documented AWS metadata requests using the current operator session')
    parser.add_argument('--output', type=Path, default=Path('reports/cloud-readiness.json'))
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text()) if args.config else {}
        checks = validate_config(config)
    except (OSError, ValueError):
        config = {}
        checks = [result('configuration', False, 'The supplied file could not be read as a JSON object; its contents were not echoed.')]
    config_valid = all(c['status'] == 'pass' for c in checks)
    aws_checked = False
    if args.aws_read_only and config_valid:
        if not shutil.which('aws'):
            checks.append(result('aws_cli', False, 'Install AWS CLI v2 before requesting metadata checks.'))
        else:
            aws_checked = True
            checks.extend(validate_aws(config, AwsMetadata(config['aws_region'])))
    checks_passed = all(c['status'] == 'pass' for c in checks)
    report = {
        'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'mode': 'aws-metadata' if args.aws_read_only else 'offline',
        'configuration_valid': config_valid,
        'aws_metadata_checked': aws_checked,
        'checks_passed': checks_passed,
        'deployment_ready': False,
        'readiness': 'blocked' if not checks_passed else ('metadata_verified' if aws_checked else 'configuration_valid_unverified'),
        'checks': checks,
        'unverified_requirements': [
            'An offline run does not verify AWS resources, credentials, DNS, or identity-provider availability.',
            'Metadata checks do not retrieve or validate the runtime secret password, execute migrations, or test database grants.',
            'The Linux amd64 image must pass the release pipeline; existence in ECR alone is not vulnerability or architecture verification.',
            'Review the Terraform plan, regional RDS capacity/quotas, cloud costs, HTTPS/DNS routing, identity-provider audience and roles, and GitHub environment protections.',
            'Prove the deployed migration, service readiness, alert delivery, and recovery workflow before declaring the deployment ready.',
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if checks_passed else 1


if __name__ == '__main__':
    sys.exit(main())
