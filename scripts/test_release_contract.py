#!/usr/bin/env python3
"""Immutable image compatibility and extraction ownership failure-path tests."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

import release_contract as contract

IMAGE = 'sha256:' + 'a' * 64
OLD_IMAGE = 'sha256:' + 'b' * 64
REPOSITORY = '123456789012.dkr.ecr.us-east-1.amazonaws.com/inventory'
REFERENCE = REPOSITORY + '@' + IMAGE
VALID = {'schemaVersion': 1, 'service': 'inventory-cloud-service',
         'capabilities': sorted(contract.REQUIRED_CAPABILITIES)}


class DockerFixture:
    def __init__(self, mode='valid'):
        self.mode = mode
        self.calls = []
        self.containers = {}

    def run(self, args, **kwargs):
        self.calls.append(args)
        command = args[1:]
        if command[:1] == ['pull']:
            return ''
        if command[:2] == ['image', 'inspect']:
            return json.dumps([{'Id': IMAGE, 'Os': 'linux',
                                'Architecture': 'arm64' if self.mode == 'wrong-platform' else 'amd64',
                                'RepoDigests': [REFERENCE + '-wrong' if self.mode == 'wrong-digest' else REFERENCE]}])
        if command[:1] == ['create']:
            name = command[command.index('--name') + 1]
            label, ownership = command[command.index('--label') + 1].split('=', 1)
            self.containers[name] = {'Name': '/' + name, 'Image': IMAGE, 'Config': {'Labels': {label: ownership}}}
            if self.mode == 'uncertain-create':
                raise contract.ContractError('Create response lost')
            if self.mode == 'foreign-container':
                self.containers[name]['Config']['Labels'][label] = 'unrelated-operation'
                raise contract.ContractError('Name collision')
            if self.mode == 'wrong-cleanup-image':
                self.containers[name]['Image'] = OLD_IMAGE
            if self.mode == 'wrong-cleanup-name':
                self.containers[name]['Name'] = '/unrelated-container'
            return name
        if command[:1] == ['cp']:
            destination = Path(command[-1])
            if self.mode == 'missing-contract':
                raise contract.ContractError('Copy failed')
            if self.mode == 'symlink-contract':
                destination.symlink_to('/etc/passwd')
            elif self.mode == 'duplicate-json-key':
                destination.write_text('{"schemaVersion":2,"schemaVersion":1,"service":"inventory-cloud-service","capabilities":' + json.dumps(VALID['capabilities']) + '}')
            elif self.mode == 'malformed-contract':
                destination.write_text('{truncated')
            else:
                destination.write_text(json.dumps(VALID))
            return ''
        if command[:2] == ['container', 'ls']:
            name = command[command.index('--filter') + 1].removeprefix('name=')
            return name if name in self.containers else ''
        if command[:2] == ['container', 'inspect']:
            return json.dumps([self.containers[command[-1]]])
        if command[:1] == ['rm']:
            del self.containers[command[-1]]
            return ''
        raise AssertionError('Unexpected command: ' + repr(args))


class ReleaseContractTest(unittest.TestCase):
    def test_repository_contract_matches_fixed_policy(self):
        actual = json.loads(Path(__file__).parent.parent.joinpath('release-contract.json').read_text())
        self.assertEqual(VALID, contract.validate_contract(actual))

    def test_valid_local_image_uses_immutable_id_without_starting_application(self):
        fixture = DockerFixture()
        with patch.object(contract, 'run_checked', side_effect=fixture.run):
            report = contract.inspect_local_contract(IMAGE)
        self.assertEqual(IMAGE, report['image_id'])
        self.assertEqual('linux/amd64', report['platform'])
        self.assertEqual({}, fixture.containers)
        self.assertFalse(any(call[1] in ('run', 'start', 'exec') for call in fixture.calls))
        create = next(call for call in fixture.calls if call[1] == 'create')
        self.assertEqual(IMAGE, create[-1])
        self.assertEqual('none', create[create.index('--network') + 1])

    def test_registry_inspection_checks_requested_digest_and_platform(self):
        fixture = DockerFixture()
        with patch.object(contract, 'run_checked', side_effect=fixture.run):
            report = contract.inspect_registry_contract(REFERENCE, {})
        self.assertEqual(REFERENCE, report['image'])
        self.assertIn(['docker', 'pull', '--platform', 'linux/amd64', REFERENCE], fixture.calls)

    def test_tags_are_rejected_before_any_command(self):
        with patch.object(contract, 'run_checked') as run:
            for ref in ('inventory:latest', REFERENCE):
                with self.assertRaises(contract.ContractError):
                    contract.inspect_local_contract(ref)
            with self.assertRaises(contract.ContractError):
                contract.inspect_registry_contract(REPOSITORY + ':latest', {})
            run.assert_not_called()

    def test_wrong_registry_digest_and_platform_block_before_extraction(self):
        for mode in ('wrong-digest', 'wrong-platform'):
            with self.subTest(mode=mode):
                fixture = DockerFixture(mode)
                with patch.object(contract, 'run_checked', side_effect=fixture.run):
                    with self.assertRaises(contract.ContractError):
                        contract.inspect_registry_contract(REFERENCE, {})
                self.assertFalse(any(call[1] == 'create' for call in fixture.calls))

    def test_missing_invalid_or_symlink_contract_always_cleans_owned_container(self):
        for mode in ('missing-contract', 'malformed-contract', 'symlink-contract', 'duplicate-json-key', 'uncertain-create'):
            with self.subTest(mode=mode):
                fixture = DockerFixture(mode)
                with patch.object(contract, 'run_checked', side_effect=fixture.run):
                    with self.assertRaises(contract.ContractError):
                        contract.inspect_local_contract(IMAGE)
                self.assertEqual({}, fixture.containers)
                self.assertTrue(any(call[1] == 'rm' for call in fixture.calls))

    def test_foreign_name_label_or_image_is_not_removed(self):
        for mode in ('foreign-container', 'wrong-cleanup-name', 'wrong-cleanup-image'):
            with self.subTest(mode=mode):
                fixture = DockerFixture(mode)
                with patch.object(contract, 'run_checked', side_effect=fixture.run):
                    with self.assertRaises(contract.ContractError):
                        contract.inspect_local_contract(IMAGE)
                self.assertFalse(any(call[1] == 'rm' for call in fixture.calls))
                self.assertEqual(1, len(fixture.containers))

    def test_exact_schema_service_and_capabilities_are_required(self):
        invalid = [
            {**VALID, 'schemaVersion': True}, {**VALID, 'schemaVersion': 2},
            {**VALID, 'service': 'unrelated-service'},
            {**VALID, 'capabilities': VALID['capabilities'][:-1]},
            {**VALID, 'capabilities': VALID['capabilities'] + ['extra/v2']},
            {**VALID, 'capabilities': VALID['capabilities'] + [VALID['capabilities'][0]]},
            {**VALID, 'extra': 'untrusted-caller-field'},
        ]
        for data in invalid:
            with self.subTest(data=data):
                with self.assertRaises(contract.ContractError):
                    contract.validate_contract(data)


SPEC = importlib.util.spec_from_file_location('verify_deployment_image', Path(__file__).with_name('verify-deployment-image.py'))
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)


class DeploymentCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.environment = {'AWS_REGION': 'us-east-1', 'ECS_CLUSTER': 'inventory', 'ECS_SERVICE': 'inventory',
                            'ECR_REPOSITORY': 'inventory', 'IMAGE_DIGEST': IMAGE, 'DOCKER_HOST': 'unix:///fixture.sock'}
        self.service = {'status': 'ACTIVE', 'taskDefinition': 'arn:aws:ecs:us-east-1:123456789012:task-definition/inventory:1',
                        'desiredCount': 1, 'runningCount': 1, 'pendingCount': 0}
        self.service['deployments'] = [{'id':'ecs-svc/original','status':'PRIMARY','rolloutState':'COMPLETED',
                                        'taskDefinition':self.service['taskDefinition'], 'desiredCount':1,'runningCount':1,'pendingCount':0}]
        self.calls = []
        self.auth_paths = []

    def aws(self, region, *args):
        self.calls.append(args)
        if args[:2] == ('ecr', 'describe-repositories'):
            return REPOSITORY
        if args[:2] == ('ecs', 'describe-services'):
            return json.dumps({'services': [self.service], 'failures': []})
        if args[:2] == ('ecs', 'describe-task-definition'):
            return json.dumps({'containerDefinitions': [{'name': 'inventory', 'image': REPOSITORY + '@' + OLD_IMAGE}]})
        if args[:2] == ('ecr', 'get-login-password'):
            return 'PRIVATE_REGISTRY_PASSWORD'
        raise AssertionError('Unexpected AWS command')

    def inspect(self, image, environment):
        auth = Path(environment['DOCKER_CONFIG'])
        self.assertTrue(auth.is_dir())
        self.assertEqual(0o700, stat.S_IMODE(auth.stat().st_mode))
        self.auth_paths.append(auth)
        return {'image': image, 'capabilities': sorted(contract.REQUIRED_CAPABILITIES)}

    def test_candidate_and_previous_image_are_checked_and_private_login_removed(self):
        with patch.object(DEPLOY, 'aws', side_effect=self.aws), patch.object(DEPLOY, 'run_checked', return_value=''), patch.object(DEPLOY, 'inspect_registry_contract', side_effect=self.inspect) as inspect:
            result = DEPLOY.verify_deployment(self.environment)
        self.assertEqual([REFERENCE, REPOSITORY + '@' + OLD_IMAGE], [call.args[0] for call in inspect.call_args_list])
        self.assertFalse(result['bootstrap_without_running_baseline'])
        self.assertTrue(all(not path.exists() for path in self.auth_paths))
        self.assertNotIn('PRIVATE_REGISTRY_PASSWORD', json.dumps(result))

    def test_pending_tasks_block_unsettled_zero_desired_service(self):
        self.service.update(desiredCount=0, runningCount=0, pendingCount=1)
        with patch.object(DEPLOY, 'aws', side_effect=self.aws), patch.object(DEPLOY, 'inspect_registry_contract') as inspect:
            with self.assertRaises(contract.ContractError):
                DEPLOY.verify_deployment(self.environment)
        inspect.assert_not_called()

    def test_only_confirmed_zero_counts_allow_bootstrap_without_old_contract(self):
        self.service.update(desiredCount=0, runningCount=0, pendingCount=0)
        self.service['deployments'][0].update(desiredCount=0, runningCount=0, pendingCount=0)
        with patch.object(DEPLOY, 'aws', side_effect=self.aws), patch.object(DEPLOY, 'run_checked', return_value=''), patch.object(DEPLOY, 'inspect_registry_contract', side_effect=self.inspect) as inspect:
            result = DEPLOY.verify_deployment(self.environment)
        self.assertEqual(1, inspect.call_count)
        self.assertTrue(result['bootstrap_without_running_baseline'])

    def test_multiple_deployments_block_even_if_service_counts_look_stable(self):
        self.service['deployments'].append({**self.service['deployments'][0], 'id':'ecs-svc/older',
                                            'status':'ACTIVE', 'taskDefinition':'arn:aws:ecs:us-east-1:123456789012:task-definition/inventory:0'})
        with patch.object(DEPLOY, 'aws', side_effect=self.aws), patch.object(DEPLOY, 'inspect_registry_contract') as inspect:
            with self.assertRaises(contract.ContractError):
                DEPLOY.verify_deployment(self.environment)
        inspect.assert_not_called()

    def test_rollout_state_primary_role_and_deployment_counts_must_be_confirmed(self):
        original = dict(self.service['deployments'][0])
        for override in ({'rolloutState':'IN_PROGRESS'}, {'rolloutState':'FAILED'}, {'status':'ACTIVE'},
                         {'taskDefinition':'different-task'}, {'runningCount':0}, {'pendingCount':1}, {'id':''}):
            with self.subTest(override=override):
                self.service['deployments'][0] = {**original, **override}
                with patch.object(DEPLOY, 'aws', side_effect=self.aws), patch.object(DEPLOY, 'inspect_registry_contract') as inspect:
                    with self.assertRaises(contract.ContractError):
                        DEPLOY.verify_deployment(self.environment)
                inspect.assert_not_called()

    def test_zero_count_bootstrap_requires_completed_deployment(self):
        self.service.update(desiredCount=0, runningCount=0, pendingCount=0)
        self.service['deployments'][0].update(desiredCount=0, runningCount=0, pendingCount=0, rolloutState='IN_PROGRESS')
        with patch.object(DEPLOY, 'aws', side_effect=self.aws), patch.object(DEPLOY, 'inspect_registry_contract') as inspect:
            with self.assertRaises(contract.ContractError):
                DEPLOY.verify_deployment(self.environment)
        inspect.assert_not_called()

    def test_private_login_is_removed_on_contract_failure(self):
        def fail(image, environment):
            self.auth_paths.append(Path(environment['DOCKER_CONFIG']))
            raise contract.ContractError('Incompatible contract')
        with patch.object(DEPLOY, 'aws', side_effect=self.aws), patch.object(DEPLOY, 'run_checked', return_value=''), patch.object(DEPLOY, 'inspect_registry_contract', side_effect=fail):
            with self.assertRaises(contract.ContractError):
                DEPLOY.verify_deployment(self.environment)
        self.assertTrue(all(not path.exists() for path in self.auth_paths))


if __name__ == '__main__':
    unittest.main()
