#!/usr/bin/env python3
"""Enforce candidate and live rollback-image compatibility before ECS mutations."""
import json
import os
import re
import sys
import tempfile

from release_contract import ContractError, IMAGE_ID, REGISTRY_IMAGE, inspect_registry_contract, run_checked


def aws(region, *arguments):
    return run_checked(['aws', *arguments, '--region', region, '--no-cli-pager'], timeout=60)


def verify_deployment(environment):
    names = ('AWS_REGION', 'ECS_CLUSTER', 'ECS_SERVICE', 'ECR_REPOSITORY', 'IMAGE_DIGEST')
    if any(not environment.get(name) for name in names):
        raise ContractError('Deployment image verification requires the documented AWS/ECS/ECR settings.')
    region = environment['AWS_REGION']
    digest = environment['IMAGE_DIGEST']
    if not IMAGE_ID.fullmatch(digest):
        raise ContractError('The requested image digest is invalid.')
    repository = aws(region, 'ecr', 'describe-repositories', '--repository-names', environment['ECR_REPOSITORY'],
                     '--query', 'repositories[0].repositoryUri', '--output', 'text').strip()
    candidate = f'{repository}@{digest}'
    if not REGISTRY_IMAGE.fullmatch(candidate) or f'.ecr.{region}.amazonaws.com/' not in candidate:
        raise ContractError('ECR returned an unexpected repository or region.')
    try:
        response = json.loads(aws(region, 'ecs', 'describe-services', '--cluster', environment['ECS_CLUSTER'],
                                  '--services', environment['ECS_SERVICE'], '--output', 'json'))
        if response['failures'] != [] or len(response['services']) != 1:
            raise ValueError('Service lookup failed')
        service = response['services'][0]
        if service['status'] != 'ACTIVE':
            raise ValueError('Service is not active')
        previous_count = service['desiredCount']
        running_count = service['runningCount']
        pending_count = service['pendingCount']
        previous_task = service['taskDefinition']
        if any(type(value) is not int or value < 0 for value in (previous_count, running_count, pending_count)) or not isinstance(previous_task, str):
            raise ValueError('Invalid service snapshot')
        if not re.fullmatch(r'arn:aws:ecs:[a-z0-9-]+:\d{12}:task-definition/[A-Za-z0-9_-]+:\d+', previous_task):
            raise ValueError('Invalid task definition')
        deployments = service['deployments']
        if not isinstance(deployments, list) or len(deployments) != 1:
            raise ContractError('The service must have one settled deployment before migration or rollout.')
        deployment = deployments[0]
        previous_deployment_id = deployment['id']
        if (not isinstance(previous_deployment_id, str) or not previous_deployment_id
                or deployment['status'] != 'PRIMARY'
                or deployment['taskDefinition'] != previous_task
                or deployment['rolloutState'] != 'COMPLETED'
                or running_count != previous_count or pending_count != 0
                or any(type(deployment[field]) is not int or deployment[field] != value
                       for field, value in (('desiredCount', previous_count), ('runningCount', running_count), ('pendingCount', pending_count)))):
            raise ContractError('The service deployment is not confirmed complete and stable; migration and rollout are blocked.')
        previous_image = None
        live_baseline = any(value > 0 for value in (previous_count, running_count, pending_count))
        if live_baseline:
            definition = json.loads(aws(region, 'ecs', 'describe-task-definition', '--task-definition', previous_task,
                                        '--query', 'taskDefinition', '--output', 'json'))
            containers = [c for c in definition['containerDefinitions'] if c.get('name') == 'inventory']
            if len(containers) != 1:
                raise ValueError('Invalid application container inventory')
            previous_image = containers[0]['image']
            if not REGISTRY_IMAGE.fullmatch(previous_image) or not previous_image.startswith(repository + '@'):
                raise ContractError('The live rollback baseline must use a digest from this application repository.')
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError('The current service and rollback baseline could not be determined.') from exc

    # Never reuse or change the operator's Docker login. No credentials enter reports.
    with tempfile.TemporaryDirectory(prefix='inventory-registry-auth-') as temporary:
        os.chmod(temporary, 0o700)
        docker_env = {**environment, 'DOCKER_CONFIG': temporary}
        if not docker_env.get('DOCKER_HOST'):
            docker_env['DOCKER_HOST'] = run_checked(
                ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
                environment=environment).strip()
        docker_env.pop('DOCKER_CONTEXT', None)
        password = aws(region, 'ecr', 'get-login-password')
        try:
            run_checked(['docker', 'login', '--username', 'AWS', '--password-stdin', repository.split('/')[0]],
                        environment=docker_env, input_text=password)
        finally:
            password = None
        selected = inspect_registry_contract(candidate, docker_env)
        baseline = inspect_registry_contract(previous_image, docker_env) if previous_image else None
    return {'candidate': selected, 'previous_task': previous_task, 'previous_count': previous_count,
            'previous_running_count': running_count, 'previous_pending_count': pending_count,
            'previous_deployment_id': previous_deployment_id,
            'rollback_baseline': baseline, 'bootstrap_without_running_baseline': not live_baseline}


def main():
    try:
        report = verify_deployment(os.environ.copy())
    except (ContractError, OSError) as exc:
        print(f'Image compatibility gate blocked deployment: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
