#!/usr/bin/env python3
"""Inspect embedded release compatibility without starting the application's process."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid
import sys

REQUIRED_CAPABILITIES = frozenset({
    'separate-migrations/v1',
    'restricted-runtime/v1',
    'reservation-idempotency/v1',
})
IMAGE_ID = re.compile(r'^sha256:[a-f0-9]{64}$')
REGISTRY_IMAGE = re.compile(r'^(\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9]+(?:[._/-][a-z0-9]+)*)@(sha256:[a-f0-9]{64})$')
OWNERSHIP_LABEL = 'com.inventory.release-contract.operation'


class ContractError(RuntimeError):
    """Compatibility could not be proven; do not migrate or roll out."""


def run_checked(arguments, *, environment=None, input_text=None, timeout=120):
    try:
        completed = subprocess.run(arguments, text=True, capture_output=True, input=input_text,
                                   env=environment, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError('A required image inspection command could not complete.') from exc
    if completed.returncode != 0:
        # Avoid echoing registry login data, arbitrary embedded content, or CLI error output.
        raise ContractError('A required image inspection command failed; compatibility was not verified.')
    return completed.stdout


def validate_contract(contract):
    if not isinstance(contract, dict) or set(contract) != {'schemaVersion', 'service', 'capabilities'}:
        raise ContractError('The embedded release contract has an unsupported structure.')
    if type(contract['schemaVersion']) is not int or contract['schemaVersion'] != 1:
        raise ContractError('The embedded release contract schema is unsupported.')
    if contract['service'] != 'inventory-cloud-service':
        raise ContractError('The embedded contract identifies a different service.')
    capabilities = contract['capabilities']
    if (not isinstance(capabilities, list) or not all(isinstance(value, str) for value in capabilities)
            or len(capabilities) != len(set(capabilities))
            or set(capabilities) != REQUIRED_CAPABILITIES):
        raise ContractError('The image does not declare the exact required migration, runtime, and idempotency capabilities.')
    return {'schemaVersion': 1, 'service': 'inventory-cloud-service',
            'capabilities': sorted(REQUIRED_CAPABILITIES)}


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError('The embedded contract contains duplicate JSON object keys.')
        result[key] = value
    return result


def _remove_owned_container(name, ownership, image_id, docker_env):
    identifiers = run_checked(['docker', 'container', 'ls', '--all', '--filter', f'name={name}',
                               '--format', '{{.ID}}'], environment=docker_env).split()
    if not identifiers:
        return
    if len(identifiers) != 1:
        raise ContractError('Extraction container ownership is ambiguous; no container was removed.')
    try:
        containers = json.loads(run_checked(['docker', 'container', 'inspect', name], environment=docker_env))
        container = containers[0]
        if (len(containers) != 1 or container['Name'] != '/' + name
                or container['Config']['Labels'].get(OWNERSHIP_LABEL) != ownership
                or container.get('Image') != image_id):
            raise ContractError('Extraction container ownership did not match; no container was removed.')
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        raise ContractError('Extraction container ownership could not be proven; no container was removed.') from exc
    run_checked(['docker', 'rm', name], environment=docker_env)


def _inspection(image_reference, expected_platform, docker_env):
    try:
        inspected = json.loads(run_checked(['docker', 'image', 'inspect', image_reference], environment=docker_env))
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise ValueError('Not exactly one image')
        image = inspected[0]
        if not IMAGE_ID.fullmatch(image['Id']):
            raise ValueError('Invalid image identity')
        if f"{image['Os']}/{image['Architecture']}" != expected_platform:
            raise ContractError('Image platform does not match the required deployment platform.')
        return image
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError('Docker did not return a valid image identity and platform.') from exc


def _extract_contract(image_id, expected_platform, docker_env):
    ownership = uuid.uuid4().hex
    container_name = 'inventory-contract-' + ownership
    container_created = False
    with tempfile.TemporaryDirectory(prefix='inventory-contract-') as temporary:
        output = Path(temporary) / 'release-contract.json'
        try:
            # The image's entrypoint never runs. Use the inspected immutable local ID.
            # Also clean by our unique name if creation succeeded but its response failed.
            container_created = True
            run_checked(['docker', 'create', '--name', container_name, '--network', 'none',
                         '--label', f'{OWNERSHIP_LABEL}={ownership}',
                         '--platform', expected_platform, '--entrypoint', '/bin/true', image_id], environment=docker_env)
            run_checked(['docker', 'cp', f'{container_name}:/app/release-contract.json', str(output)], environment=docker_env)
            if output.is_symlink() or not output.is_file() or output.stat().st_size > 16384:
                raise ContractError('The embedded contract must be a small regular JSON file.')
            raw = output.read_bytes()
            try:
                validated = validate_contract(json.loads(raw, object_pairs_hook=strict_object))
            except (ValueError, UnicodeError) as exc:
                raise ContractError('The embedded release contract is not valid JSON.') from exc
            return {**validated, 'contract_sha256': hashlib.sha256(raw).hexdigest()}
        finally:
            if container_created:
                # No force flag: the extraction container is never started.
                _remove_owned_container(container_name, ownership, image_id, docker_env)


def inspect_local_contract(image_id, expected_platform='linux/amd64', docker_env=None):
    """For isolated local drills: require an immutable ID and inspect the actual image."""
    if not IMAGE_ID.fullmatch(image_id):
        raise ContractError('Local compatibility inspection requires a full immutable image ID.')
    environment = docker_env or os.environ.copy()
    image = _inspection(image_id, expected_platform, environment)
    if image['Id'] != image_id:
        raise ContractError('The inspected local image differs from the requested immutable ID.')
    return {'image_id': image_id, 'platform': expected_platform,
            **_extract_contract(image_id, expected_platform, environment)}


def inspect_registry_contract(image_reference, docker_env):
    """Production policy is fixed: digest-pinned private ECR image on Linux amd64."""
    if not REGISTRY_IMAGE.fullmatch(image_reference):
        raise ContractError('A digest-pinned private ECR image is required.')
    run_checked(['docker', 'pull', '--platform', 'linux/amd64', image_reference],
                environment=docker_env, timeout=120)
    image = _inspection(image_reference, 'linux/amd64', docker_env)
    if image_reference not in image.get('RepoDigests', []):
        raise ContractError('The pulled image does not match the requested repository digest.')
    return {'image': image_reference, 'image_id': image['Id'], 'platform': 'linux/amd64',
            **_extract_contract(image['Id'], 'linux/amd64', docker_env)}


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ContractError('Supply exactly one immutable image ID for Linux amd64 compatibility inspection.')
        print(json.dumps(inspect_local_contract(sys.argv[1]), indent=2))
    except (ContractError, OSError) as exc:
        print(f'Image compatibility verification failed: {exc}', file=sys.stderr)
        sys.exit(1)
