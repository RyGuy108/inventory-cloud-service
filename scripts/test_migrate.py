#!/usr/bin/env python3
"""Offline migration release-gate tests. Every AWS request uses a local stub."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from release_contract_fixtures import DOCKER_STUB


TASK_ARN = 'arn:aws:ecs:us-east-1:123456789012:task/inventory/migration-123'
DIGEST = 'sha256:' + 'a' * 64
REPOSITORY = '123456789012.dkr.ecr.us-east-1.amazonaws.com/inventory'
SECRETS = [
    {'name': 'DB_PASSWORD', 'valueFrom': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:owner'},
    {'name': 'DB_APP_PASSWORD', 'valueFrom': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:runtime'},
]

AWS_STUB = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
directory = pathlib.Path(os.environ['MIGRATE_TEST_DIR'])
state_file = directory / 'state.json'
state = json.loads(state_file.read_text())
args = sys.argv[1:]
state['calls'].append(args)
state_file.write_text(json.dumps(state))
command = ' '.join(args[:2])
mode = os.environ['MIGRATE_TEST_MODE']
task = os.environ['MIGRATE_TEST_TASK_ARN']
old = 'arn:aws:ecs:us-east-1:123456789012:task-definition/inventory-migration:1'
new = 'arn:aws:ecs:us-east-1:123456789012:task-definition/inventory-migration:2'
if command == 'ecr describe-repositories':
    print('123456789012.dkr.ecr.us-east-1.amazonaws.com/inventory')
elif command == 'ecr describe-images':
    print('{}')
elif command == 'ecr get-login-password':
    print('OFFLINE_LOGIN_SECRET_MUST_NOT_BE_ECHOED')
elif command == 'ecs describe-task-definition':
    if args[args.index('--task-definition') + 1].endswith('/inventory:1'):
        print(json.dumps({'containerDefinitions': [{'name':'inventory', 'image':'123456789012.dkr.ecr.us-east-1.amazonaws.com/inventory@sha256:' + 'b' * 64}]}))
        sys.exit(0)
    print(json.dumps({
        'family':'inventory-migration', 'taskDefinitionArn':old, 'revision':1,
        'status':'ACTIVE', 'requiresAttributes':[], 'compatibilities':['FARGATE'],
        'registeredAt':'2026-01-01', 'registeredBy':'test',
        'executionRoleArn':'execution-role', 'taskRoleArn':'migration-role',
        'networkMode':'awsvpc', 'cpu':'256', 'memory':'512',
        'containerDefinitions':[
            {'name':'migration', 'image':'old-image', 'essential':True,
             'environment':[{'name':'SPRING_PROFILES_ACTIVE', 'value':'migrate'}],
             'secrets':json.loads(os.environ['MIGRATE_TEST_SECRETS'])},
            {'name':'sidecar', 'image':'unchanged', 'essential':False}]}))
elif command == 'ecs register-task-definition':
    source = args[args.index('--cli-input-json') + 1].removeprefix('file://')
    (directory / 'registered.json').write_text(pathlib.Path(source).read_text())
    print(new)
elif command == 'ecs describe-services':
    if '--query' not in args:
        service = {'taskDefinition':'arn:aws:ecs:us-east-1:123456789012:task-definition/inventory:1',
                   'desiredCount':1,'runningCount':1,'pendingCount':0,'status':'ACTIVE',
                   'networkConfiguration':{'awsvpcConfiguration':{'subnets':['subnet-private'],
                      'securityGroups':['sg-database-client'],'assignPublicIp':'DISABLED'}}}
        service['deployments'] = [{'id':'ecs-svc/original','status':'PRIMARY','rolloutState':'COMPLETED',
                                   'taskDefinition':service['taskDefinition'],'desiredCount':1,'runningCount':1,'pendingCount':0}]
        snapshots = [call for call in state['calls'] if call[:2] == ['ecs','describe-services'] and '--query' not in call]
        if len(snapshots) > 1:
            if mode == 'deployment-id-drift':
                service['deployments'][0]['id'] = 'ecs-svc/replacement'
            elif mode == 'deployment-rollout-drift':
                service['deployments'][0]['rolloutState'] = 'IN_PROGRESS'
            elif mode == 'deployment-added':
                service['deployments'].append({**service['deployments'][0],'status':'ACTIVE','id':'ecs-svc/older'})
        if mode == 'baseline-drift' and len(snapshots) > 1:
            service['taskDefinition'] = 'arn:aws:ecs:us-east-1:123456789012:task-definition/inventory:2'
        print(json.dumps({'services':[service], 'failures':[]}))
        sys.exit(0)
    print(json.dumps({'awsvpcConfiguration':{
        'subnets':['subnet-private'], 'securityGroups':['sg-database-client'],
        'assignPublicIp':'DISABLED'}}))
elif command == 'ecs run-task':
    source = args[args.index('--network-configuration') + 1].removeprefix('file://')
    (directory / 'network.json').write_text(pathlib.Path(source).read_text())
    result = {'failures':[], 'tasks':[{'taskArn':task}]}
    if mode == 'scheduling-failure':
        result['failures'] = [{'arn':'capacity', 'reason':'RESOURCE:MEMORY'}]
        result['tasks'] = []
    elif mode == 'missing-scheduled-task':
        result['tasks'] = []
    print(json.dumps(result))
elif command == 'ecs wait':
    if mode == 'wait-failure':
        sys.exit(255)
elif command == 'ecs stop-task':
    print('{}')
elif command == 'ecs describe-tasks':
    if mode == 'completion-read-failure':
        sys.exit(254)
    container = {'name':'migration', 'exitCode':0}
    if mode == 'nonzero-exit':
        container['exitCode'] = 1
    elif mode == 'missing-exit':
        del container['exitCode']
    result = {'failures':[], 'tasks':[{
        'taskArn':task, 'lastStatus':'STOPPED', 'containers':[container]}]}
    if mode == 'completion-api-failure':
        result['failures'] = [{'arn':task, 'reason':'MISSING'}]
    elif mode == 'missing-completed-task':
        result['tasks'] = []
    elif mode == 'still-running':
        result['tasks'][0]['lastStatus'] = 'RUNNING'
    print(json.dumps(result))
else:
    raise SystemExit('Unexpected AWS command: ' + repr(args))
'''


class MigrationScriptTest(unittest.TestCase):
    def run_scenario(self, mode):
        with tempfile.TemporaryDirectory(prefix='inventory-migration-test-') as temporary:
            directory = Path(temporary)
            (directory / 'state.json').write_text(json.dumps({'calls': []}))
            (directory / 'aws').write_text(AWS_STUB)
            (directory / 'docker').write_text(DOCKER_STUB)
            (directory / 'docker').chmod(0o755)
            (directory / 'aws').chmod(0o755)
            result = subprocess.run(
                ['bash', str(Path(__file__).with_name('migrate-ecs.sh'))],
                env={**os.environ, 'PATH': f'{directory}:{os.environ["PATH"]}',
                     'MIGRATE_TEST_DIR': temporary, 'MIGRATE_TEST_MODE': mode,
                     'MIGRATE_TEST_TASK_ARN': TASK_ARN,
                     'MIGRATE_TEST_SECRETS': json.dumps(SECRETS),
                     'AWS_REGION': 'us-east-1', 'ECS_CLUSTER': 'inventory',
                     'ECS_SERVICE': 'inventory', 'ECR_REPOSITORY': 'inventory',
                     'IMAGE_DIGEST': DIGEST,
                     'MIGRATION_TASK_DEFINITION': 'inventory-migration:1'},
                capture_output=True, text=True, timeout=15)
            state = json.loads((directory / 'state.json').read_text())
            self.assertNotIn('OFFLINE_LOGIN_SECRET_MUST_NOT_BE_ECHOED', result.stdout + result.stderr)
            docker_state = directory / 'docker-state.json'
            if docker_state.exists():
                self.assertEqual({}, json.loads(docker_state.read_text())['containers'])
            registered = json.loads((directory / 'registered.json').read_text()) if (directory / 'registered.json').exists() else None
            network = json.loads((directory / 'network.json').read_text()) if (directory / 'network.json').exists() else None
            self.assertFalse(any(call[:2] == ['ecs', 'update-service'] for call in state['calls']),
                             'A migration job must never update the application service')
            self.assertFalse(any(call[:2] == ['ecs', 'delete-service'] for call in state['calls']))
            return result, state['calls'], registered, network

    def test_success_uses_release_digest_preserves_secrets_and_private_network(self):
        result, calls, registered, network = self.run_scenario('success')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        migration, sidecar = registered['containerDefinitions']
        self.assertEqual(migration['image'], REPOSITORY + '@' + DIGEST)
        self.assertEqual(migration['secrets'], SECRETS)
        self.assertEqual(migration['environment'], [{'name': 'SPRING_PROFILES_ACTIVE', 'value': 'migrate'}])
        self.assertEqual(sidecar['image'], 'unchanged')
        self.assertEqual(registered['taskRoleArn'], 'migration-role')
        self.assertEqual(registered['executionRoleArn'], 'execution-role')
        for field in ('taskDefinitionArn', 'revision', 'status', 'requiresAttributes',
                      'compatibilities', 'registeredAt', 'registeredBy'):
            self.assertNotIn(field, registered)
        self.assertEqual(network['awsvpcConfiguration']['subnets'], ['subnet-private'])
        self.assertEqual(network['awsvpcConfiguration']['assignPublicIp'], 'DISABLED')
        run_call = next(call for call in calls if call[:2] == ['ecs', 'run-task'])
        self.assertTrue(run_call[run_call.index('--task-definition') + 1].endswith(':2'))
        self.assertEqual(run_call[run_call.index('--count') + 1], '1')
        self.assertIn('--client-token', run_call)
        read_call = next(call for call in calls if call[:2] == ['ecs', 'describe-tasks'])
        self.assertEqual(read_call[read_call.index('--tasks') + 1], TASK_ARN)
        self.assertIn('release may proceed', result.stdout)

    def test_scheduler_failures_block_release_before_wait(self):
        self.assert_scheduling_failure('scheduling-failure')

    def test_missing_scheduled_task_blocks_release_before_wait(self):
        self.assert_scheduling_failure('missing-scheduled-task')

    def test_nonzero_container_exit_blocks_release(self):
        self.assert_completion_failure('nonzero-exit')

    def test_missing_exit_code_is_not_treated_as_success(self):
        self.assert_completion_failure('missing-exit')

    def test_wait_failure_stops_the_exact_migration_task_and_blocks_release(self):
        result, calls, _, _ = self.run_scenario('wait-failure')
        self.assertNotEqual(result.returncode, 0)
        stops = [call for call in calls if call[:2] == ['ecs', 'stop-task']]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0][stops[0].index('--task') + 1], TASK_ARN)
        self.assertEqual(stops[0][stops[0].index('--cluster') + 1], 'inventory')
        self.assertFalse(any(call[:2] == ['ecs', 'describe-tasks'] for call in calls))
        self.assertNotIn('release may proceed', result.stdout)

    def test_failed_completion_read_blocks_release(self):
        self.assert_completion_failure('completion-read-failure')

    def test_completion_api_failure_entry_blocks_release(self):
        self.assert_completion_failure('completion-api-failure')

    def test_missing_completed_task_blocks_release(self):
        self.assert_completion_failure('missing-completed-task')

    def test_task_must_be_stopped_even_if_exit_code_is_present(self):
        self.assert_completion_failure('still-running')

    def test_incompatible_candidate_blocks_before_migration_mutation(self):
        self.assert_contract_block('candidate-contract-missing')

    def test_incompatible_live_baseline_blocks_before_migration_mutation(self):
        self.assert_contract_block('baseline-contract-missing')

    def test_changed_baseline_aborts_before_registering_or_running_migrations(self):
        result, calls, registered, network = self.run_scenario('baseline-drift')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(registered)
        self.assertIsNone(network)
        self.assertFalse(any(call[:2] in (['ecs', 'register-task-definition'], ['ecs', 'run-task']) for call in calls))
        self.assertIn('Service changed after image verification', result.stdout)

    def test_deployment_identity_or_rollout_drift_aborts_before_migration(self):
        for mode in ('deployment-id-drift', 'deployment-rollout-drift', 'deployment-added'):
            with self.subTest(mode=mode):
                result, calls, registered, network = self.run_scenario(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(registered)
                self.assertIsNone(network)
                self.assertFalse(any(call[:2] in (['ecs', 'register-task-definition'], ['ecs', 'run-task']) for call in calls))
                self.assertIn('Service changed after image verification', result.stdout)

    def assert_contract_block(self, mode):
        result, calls, registered, network = self.run_scenario(mode)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(registered)
        self.assertIsNone(network)
        self.assertFalse(any(call[:2] in (['ecs', 'register-task-definition'], ['ecs', 'run-task']) for call in calls))

    def assert_scheduling_failure(self, mode):
        result, calls, _, _ = self.run_scenario(mode)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[:2] == ['ecs', 'wait'] for call in calls))
        self.assertFalse(any(call[:2] == ['ecs', 'describe-tasks'] for call in calls))
        self.assertNotIn('release may proceed', result.stdout)

    def assert_completion_failure(self, mode):
        result, calls, _, _ = self.run_scenario(mode)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any(call[:2] == ['ecs', 'describe-tasks'] for call in calls))
        self.assertNotIn('release may proceed', result.stdout)


if __name__ == '__main__':
    unittest.main()
