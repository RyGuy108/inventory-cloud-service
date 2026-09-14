"""Offline Docker fixture shared by operational script tests; never imports in production."""
DOCKER_STUB = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ.get('DEPLOY_TEST_DIR') or os.environ['MIGRATE_TEST_DIR'])
mode = os.environ.get('DEPLOY_TEST_MODE') or os.environ.get('MIGRATE_TEST_MODE', '')
args = sys.argv[1:]
state_path = root / 'docker-state.json'
state = json.loads(state_path.read_text()) if state_path.exists() else {'containers': {}, 'calls': []}
state['calls'].append(args)
state_path.write_text(json.dumps(state))
if args[:2] == ['context', 'inspect']:
    print('unix:///offline-test.sock')
elif args[:1] == ['login']:
    sys.stdin.read()
    print('Login Succeeded')
elif args[:1] == ['pull']:
    pass
elif args[:2] == ['image', 'inspect']:
    ref = args[-1]
    identity = ref.split('@')[-1]
    print(json.dumps([{'Id': identity, 'Os': 'linux', 'Architecture': 'amd64', 'RepoDigests': [ref]}]))
elif args[:1] == ['create']:
    name = args[args.index('--name') + 1]
    label = args[args.index('--label') + 1].split('=', 1)
    state['containers'][name] = {'image':args[-1], 'label':label}
    state_path.write_text(json.dumps(state))
    print(name)
elif args[:1] == ['cp']:
    name = args[1].split(':')[0]
    source = state['containers'][name]['image']
    if (mode == 'candidate-contract-missing' and source == 'sha256:' + 'a' * 64) or (mode == 'baseline-contract-missing' and source == 'sha256:' + 'b' * 64):
        sys.exit(1)
    contract = {'schemaVersion':1,'service':'inventory-cloud-service','capabilities':[
        'separate-migrations/v1','restricted-runtime/v1','reservation-idempotency/v1']}
    pathlib.Path(args[2]).write_text(json.dumps(contract))
elif args[:2] == ['container', 'ls']:
    target = args[args.index('--filter') + 1].removeprefix('name=')
    if target in state['containers']:
        print(target)
elif args[:2] == ['container', 'inspect']:
    name = args[-1]
    record = state['containers'][name]
    print(json.dumps([{'Name':'/' + name,'Image':record['image'],'Config':{'Labels':{record['label'][0]:record['label'][1]}}}]))
elif args[:1] == ['rm']:
    state['containers'].pop(args[1], None)
    state_path.write_text(json.dumps(state))
else:
    raise SystemExit('Unexpected Docker command')
'''
