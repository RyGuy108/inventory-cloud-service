#!/usr/bin/env bash
set -euo pipefail
: "${AWS_REGION:?Set AWS_REGION}"
: "${ECS_CLUSTER:?Set ECS_CLUSTER}"
: "${ECS_SERVICE:?Set ECS_SERVICE}"
: "${ECR_REPOSITORY:?Set ECR_REPOSITORY}"
: "${IMAGE_DIGEST:?Set IMAGE_DIGEST}"
: "${MIGRATION_TASK_DEFINITION:?Set MIGRATION_TASK_DEFINITION from Terraform output}"
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Invalid image digest'; exit 1; }
# Check both the candidate and live rollback baseline before migrations can alter grants.
migration_dir=$(mktemp -d)
trap 'rm -rf "$migration_dir"' EXIT
python3 "$(dirname "$0")/verify-deployment-image.py" > "$migration_dir/compatibility.json"
repository_uri=$(aws ecr describe-repositories --repository-names "$ECR_REPOSITORY" --query 'repositories[0].repositoryUri' --output text)
aws ecr describe-images --repository-name "$ECR_REPOSITORY" --image-ids "imageDigest=$IMAGE_DIGEST" > /dev/null
aws ecs describe-task-definition --task-definition "$MIGRATION_TASK_DEFINITION" --query taskDefinition > "$migration_dir/base.json"
jq -e --arg image "$repository_uri@$IMAGE_DIGEST" '
  if ([.containerDefinitions[] | select(.name == "migration")] | length) != 1
  then error("Expected exactly one migration container") else . end
  | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities, .registeredAt, .registeredBy, .deregisteredAt)
  | (.containerDefinitions[] | select(.name == "migration") | .image) = $image
' "$migration_dir/base.json" > "$migration_dir/next.json"
aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" --output json > "$migration_dir/service.json"
jq -e --slurpfile verified "$migration_dir/compatibility.json" -f "$(dirname "$0")/service-baseline.jq" "$migration_dir/service.json" > /dev/null || {
  echo 'Service changed after image verification; no migration was started. Retry with one release operator.'; exit 1;
}
jq -e '.services[0].networkConfiguration' "$migration_dir/service.json" > "$migration_dir/network.json"
jq -e '.awsvpcConfiguration.subnets | length > 0' "$migration_dir/network.json" > /dev/null
next_task=$(aws ecs register-task-definition --cli-input-json "file://$migration_dir/next.json" --query 'taskDefinition.taskDefinitionArn' --output text)
request_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
aws ecs run-task --cluster "$ECS_CLUSTER" --task-definition "$next_task" --launch-type FARGATE \
  --platform-version 1.4.0 --count 1 --client-token "$request_id" \
  --network-configuration "file://$migration_dir/network.json" --output json > "$migration_dir/run.json"
jq -e '(.failures | length) == 0 and (.tasks | length) == 1' "$migration_dir/run.json" > /dev/null || {
  echo 'Migration task could not be scheduled; service deployment is blocked.'; exit 1;
}
task_arn=$(jq -er '.tasks[0].taskArn | select(startswith("arn:aws:ecs:"))' "$migration_dir/run.json")
echo "Migration task: $task_arn"
if ! aws ecs wait tasks-stopped --cluster "$ECS_CLUSTER" --tasks "$task_arn"; then
  aws ecs stop-task --cluster "$ECS_CLUSTER" --task "$task_arn" --reason 'Migration wait failed; block release' > /dev/null || true
  echo 'Migration completion was not confirmed; service deployment is blocked.'
  exit 1
fi
aws ecs describe-tasks --cluster "$ECS_CLUSTER" --tasks "$task_arn" --output json > "$migration_dir/completed.json"
jq -e '(.failures | length) == 0 and (.tasks | length) == 1
  and .tasks[0].lastStatus == "STOPPED"
  and ([.tasks[0].containers[] | select(.name == "migration")] | length) == 1
  and ([.tasks[0].containers[] | select(.name == "migration")][0].exitCode == 0)' \
  "$migration_dir/completed.json" > /dev/null || {
    echo 'Migration failed or has no confirmed successful exit; service deployment is blocked.'; exit 1;
}
echo 'Migration and runtime database permissions verified; release may proceed.'
