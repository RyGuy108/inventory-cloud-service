#!/usr/bin/env python3
"""Offline release-script checks. AWS and HTTPS calls are simulated; no cloud access."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from release_contract_fixtures import DOCKER_STUB


AWS_STUB = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
directory = pathlib.Path(os.environ['DEPLOY_TEST_DIR'])
state_path = directory / 'state.json'
state = json.loads(state_path.read_text())
args = sys.argv[1:]
command = ' '.join(args[:2])
mode = os.environ['DEPLOY_TEST_MODE']
old = 'arn:aws:ecs:us-east-1:123456789012:task-definition/inventory:1'
new = 'arn:aws:ecs:us-east-1:123456789012:task-definition/inventory:2'
if command == 'ecr describe-repositories':
    print('123456789012.dkr.ecr.us-east-1.amazonaws.com/inventory')
elif command == 'ecr describe-images':
    print('{}')
elif command == 'ecr get-login-password':
    print('OFFLINE_LOGIN_SECRET_MUST_NOT_BE_ECHOED')
elif command == 'ecs describe-services':
    if '--query' not in args:
        state['snapshot_calls'] = state.get('snapshot_calls', 0) + 1
        state_path.write_text(json.dumps(state))
        service = {'taskDefinition':state['current'],'desiredCount':state.get('desired_count',1),
                   'runningCount':int(os.environ['DEPLOY_TEST_LIVE_COUNT']),'pendingCount':0,'status':'ACTIVE'}
        service['deployments'] = [{'id':'ecs-svc/original','status':'PRIMARY','rolloutState':'COMPLETED',
                                   'taskDefinition':service['taskDefinition'],'desiredCount':service['desiredCount'],
                                   'runningCount':service['runningCount'],'pendingCount':0}]
        if state['snapshot_calls'] > 1:
            if mode == 'deployment-id-drift':
                service['deployments'][0]['id'] = 'ecs-svc/replacement'
            elif mode == 'deployment-rollout-drift':
                service['deployments'][0]['rolloutState'] = 'IN_PROGRESS'
            elif mode == 'deployment-added':
                service['deployments'].append({**service['deployments'][0],'status':'ACTIVE','id':'ecs-svc/older'})
        if mode == 'baseline-drift' and state['snapshot_calls'] > 1:
            service['taskDefinition'] = new
        response = {'services':[service], 'failures':[]}
        if mode == 'service-api-failure':
            response['failures'] = [{'reason':'MISSING'}]
        elif mode == 'service-inactive':
            service['status'] = 'DRAINING'
        elif mode == 'service-state-missing':
            del service['runningCount']
        print(json.dumps(response))
        sys.exit(0)
    if args[args.index('--query') + 1] == 'services[0].desiredCount':
        print(state.get('desired_count', 1))
        sys.exit(0)
    state['describe_calls'] = state.get('describe_calls', 0) + 1
    state_path.write_text(json.dumps(state))
    if mode == 'verification-read-failure' and state['describe_calls'] == 1:
        sys.exit(254)
    print(old if mode == 'automatic-rollback' else state['current'])
elif command == 'ecs describe-task-definition':
    containers = [{'name':'inventory','image':'123456789012.dkr.ecr.us-east-1.amazonaws.com/inventory@sha256:' + 'b' * 64}, {'name':'sidecar','image':'unchanged'}]
    if mode == 'missing-inventory':
        containers[0]['name'] = 'wrong-app'
    elif mode == 'duplicate-inventory':
        containers[1]['name'] = 'inventory'
    print(json.dumps({'family':'inventory','taskDefinitionArn':old,'revision':1,'status':'ACTIVE',
        'containerDefinitions': containers}))
elif command == 'ecs register-task-definition':
    state['registered_count'] = state.get('registered_count', 0) + 1
    state_path.write_text(json.dumps(state))
    path = args[args.index('--cli-input-json') + 1].removeprefix('file://')
    (directory / 'registered.json').write_text(pathlib.Path(path).read_text())
    print('None' if mode == 'invalid-registration' else new)
elif command == 'ecs update-service':
    revision = args[args.index('--task-definition') + 1]
    state['current'] = revision
    state['desired_count'] = int(args[args.index('--desired-count') + 1])
    state['updates'].append(revision)
    state_path.write_text(json.dumps(state))
    if mode == 'update-response-failure' and revision == new:
        sys.exit(254)
    print('{}')
elif command == 'ecs wait':
    if mode == 'wait-failure' and state['current'] == new:
        sys.exit(255)
else:
    raise SystemExit('Unexpected AWS command: ' + repr(args))
'''


CURL_STUB = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
mode = os.environ['DEPLOY_TEST_MODE']
if mode == 'readiness-failure':
    sys.exit(22)
args = sys.argv[1:]
output = pathlib.Path(args[args.index('--output') + 1])
body = {'status': 'DOWN' if mode == 'readiness-down' else 'UP'}
output.write_text('<html>sign in</html>' if mode == 'readiness-html' else json.dumps(body))
print('302' if mode == 'readiness-redirect' else '200', end='')
'''


class DeployScriptTest(unittest.TestCase):
    def run_scenario(self, mode, initial_count=1, live_count=None):
        with tempfile.TemporaryDirectory(prefix='inventory-deploy-test-') as temporary:
            directory = Path(temporary)
            state_file = directory / 'state.json'
            state_file.write_text(json.dumps({
                'current': 'arn:aws:ecs:us-east-1:123456789012:task-definition/inventory:1',
                'updates': [],
                'desired_count': initial_count,
            }))
            (directory / 'aws').write_text(AWS_STUB)
            (directory / 'docker').write_text(DOCKER_STUB)
            (directory / 'docker').chmod(0o755)
            (directory / 'curl').write_text(CURL_STUB)
            (directory / 'aws').chmod(0o755)
            (directory / 'curl').chmod(0o755)
            digest = 'sha256:' + 'a' * 64
            result = subprocess.run(
                ['bash', str(Path(__file__).with_name('deploy-ecs.sh'))],
                env={**os.environ, 'PATH': f'{directory}:{os.environ["PATH"]}',
                     'DEPLOY_TEST_DIR': temporary, 'DEPLOY_TEST_MODE': mode,
                     'DEPLOY_TEST_LIVE_COUNT': str(initial_count if live_count is None else live_count),
                     'AWS_REGION': 'us-east-1', 'ECS_CLUSTER': 'inventory',
                     'ECS_SERVICE': 'inventory', 'ECR_REPOSITORY': 'inventory',
                     'IMAGE_DIGEST': digest, 'PUBLIC_BASE_URL': 'https://inventory.example.com'},
                capture_output=True, text=True, timeout=15)
            self.assertNotIn('OFFLINE_LOGIN_SECRET_MUST_NOT_BE_ECHOED', result.stdout + result.stderr)
            docker_state = directory / 'docker-state.json'
            if docker_state.exists():
                self.assertEqual({}, json.loads(docker_state.read_text())['containers'])
            state = json.loads(state_file.read_text())
            if not (directory / 'registered.json').exists():
                return result, state
            registered = json.loads((directory / 'registered.json').read_text())
            self.assertNotIn('taskDefinitionArn', registered)
            self.assertNotIn('revision', registered)
            self.assertEqual(registered['containerDefinitions'][1]['image'], 'unchanged')
            self.assertTrue(registered['containerDefinitions'][0]['image'].endswith('@' + digest))
            return result, state

    def test_healthy_release_keeps_new_revision(self):
        result, state = self.run_scenario('healthy')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(state['updates']), 1)
        self.assertTrue(state['current'].endswith(':2'))

    def test_automatic_rollback_is_not_reported_as_success(self):
        self.assert_rollback('automatic-rollback')

    def test_service_wait_failure_restores_previous_revision(self):
        self.assert_rollback('wait-failure')

    def test_failed_readiness_restores_previous_revision(self):
        self.assert_rollback('readiness-failure')

    def test_failed_post_deployment_read_restores_previous_revision(self):
        self.assert_rollback('verification-read-failure')

    def test_ambiguous_update_response_restores_previous_revision(self):
        self.assert_rollback('update-response-failure')

    def test_first_deployment_starts_the_service_after_bootstrap(self):
        result, state = self.run_scenario('healthy', initial_count=0)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state['desired_count'], 1)

    def test_failed_first_deployment_returns_to_zero_tasks(self):
        result, state = self.run_scenario('readiness-failure', initial_count=0)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state['desired_count'], 0)
        self.assertTrue(state['current'].endswith(':1'))

    def test_redirect_is_not_a_successful_health_check(self):
        self.assert_rollback('readiness-redirect')

    def test_html_200_is_not_a_successful_health_check(self):
        self.assert_rollback('readiness-html')

    def test_json_down_200_is_not_a_successful_health_check(self):
        self.assert_rollback('readiness-down')

    def test_missing_inventory_container_blocks_before_update(self):
        result, state = self.run_scenario('missing-inventory')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([], state['updates'])

    def test_duplicate_inventory_container_blocks_before_update(self):
        result, state = self.run_scenario('duplicate-inventory')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([], state['updates'])

    def test_invalid_registration_response_blocks_before_update(self):
        result, state = self.run_scenario('invalid-registration')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([], state['updates'])

    def test_incompatible_candidate_blocks_before_service_mutation(self):
        result, state = self.run_scenario('candidate-contract-missing')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([], state['updates'])

    def test_incompatible_live_baseline_blocks_before_service_mutation(self):
        result, state = self.run_scenario('baseline-contract-missing')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([], state['updates'])

    def test_count_zero_bootstrap_does_not_require_an_old_image_contract(self):
        result, state = self.run_scenario('baseline-contract-missing', initial_count=0)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(1, state['desired_count'])

    def test_scaling_to_zero_does_not_bypass_live_baseline_check(self):
        result, state = self.run_scenario('baseline-contract-missing', initial_count=0, live_count=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([], state['updates'])

    def test_unconfirmed_service_state_blocks_before_mutation(self):
        for mode in ('service-api-failure', 'service-inactive', 'service-state-missing'):
            with self.subTest(mode=mode):
                result, state = self.run_scenario(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([], state['updates'])

    def test_changed_baseline_aborts_before_registering_or_updating(self):
        result, state = self.run_scenario('baseline-drift')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(0, state.get('registered_count', 0))
        self.assertEqual([], state['updates'])
        self.assertIn('Service changed after image verification', result.stdout)

    def test_deployment_identity_or_rollout_drift_aborts_before_mutation(self):
        for mode in ('deployment-id-drift', 'deployment-rollout-drift', 'deployment-added'):
            with self.subTest(mode=mode):
                result, state = self.run_scenario(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(0, state.get('registered_count', 0))
                self.assertEqual([], state['updates'])
                self.assertIn('Service changed after image verification', result.stdout)

    def assert_rollback(self, mode):
        result, state = self.run_scenario(mode)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(state['updates']), 2)
        self.assertTrue(state['current'].endswith(':1'))
        self.assertIn('restoring', result.stdout)


if __name__ == '__main__':
    unittest.main()
