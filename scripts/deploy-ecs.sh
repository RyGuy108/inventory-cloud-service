#!/usr/bin/env bash
set -euo pipefail
: "${AWS_REGION:?Set AWS_REGION}"
: "${ECS_CLUSTER:?Set ECS_CLUSTER}"
: "${ECS_SERVICE:?Set ECS_SERVICE}"
: "${ECR_REPOSITORY:?Set ECR_REPOSITORY to the repository name}"
: "${IMAGE_DIGEST:?Set IMAGE_DIGEST}"
: "${PUBLIC_BASE_URL:?Set PUBLIC_BASE_URL}"
desired_count=${DESIRED_COUNT:-1}
[[ "$desired_count" =~ ^[1-4]$ ]] || { echo 'DESIRED_COUNT must be between 1 and 4'; exit 1; }
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Invalid image digest'; exit 1; }
[[ "$PUBLIC_BASE_URL" == https://* ]] || { echo 'HTTPS URL is required'; exit 1; }
# The candidate and any live automatic-rollback baseline must be compatible.
# This gate inspects their immutable image contents before any ECS mutation.
release_dir=$(mktemp -d)
trap 'rm -rf "$release_dir"' EXIT
python3 "$(dirname "$0")/verify-deployment-image.py" > "$release_dir/compatibility.json"
repository_uri=$(aws ecr describe-repositories --repository-names "$ECR_REPOSITORY" --query 'repositories[0].repositoryUri' --output text)
aws ecr describe-images --repository-name "$ECR_REPOSITORY" --image-ids "imageDigest=$IMAGE_DIGEST" > /dev/null
previous_task=$(jq -er '.previous_task' "$release_dir/compatibility.json")
[[ "$previous_task" == arn:aws:ecs:* ]] || { echo 'Service not found'; exit 1; }
previous_count=$(jq -er '.previous_count' "$release_dir/compatibility.json")
[[ "$previous_count" =~ ^[0-9]+$ ]] || { echo 'Cannot determine previous service count'; exit 1; }
aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" --output json > "$release_dir/service.json"
jq -e --slurpfile verified "$release_dir/compatibility.json" -f "$(dirname "$0")/service-baseline.jq" "$release_dir/service.json" > /dev/null || {
  echo 'Service changed after image verification; no deployment was started. Retry with one release operator.'; exit 1;
}
aws ecs describe-task-definition --task-definition "$previous_task" --query taskDefinition > "$release_dir/previous.json"
jq -e --arg image "$repository_uri@$IMAGE_DIGEST" '
  if ([.containerDefinitions[] | select(.name == "inventory")] | length) != 1
  then error("Expected exactly one inventory container") else . end
  | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities, .registeredAt, .registeredBy, .deregisteredAt)
  | (.containerDefinitions[] | select(.name == "inventory") | .image) = $image
' "$release_dir/previous.json" > "$release_dir/next.json"
next_task=$(aws ecs register-task-definition --cli-input-json "file://$release_dir/next.json" --query 'taskDefinition.taskDefinitionArn' --output text)
[[ "$next_task" =~ ^arn:aws:ecs:[a-z0-9-]+:[0-9]{12}:task-definition/[A-Za-z0-9_-]+:[0-9]+$ ]] || {
  echo 'No valid registered task definition returned; service was not updated.'; exit 1;
}
echo "Previous task definition: $previous_task"
echo "New task definition: $next_task"
rollback() {
  echo "Deployment failed; restoring $previous_task"
  aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" --task-definition "$previous_task" --desired-count "$previous_count" > /dev/null
  aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"
}
# A failed response can follow an accepted update; restoring the known revision is safe in either case.
if ! aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" --task-definition "$next_task" --desired-count "$desired_count" > /dev/null; then
  rollback
  exit 1
fi
if ! aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"; then
  rollback
  exit 1
fi
if ! current_task=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" --query 'services[0].taskDefinition' --output text); then
  rollback
  exit 1
fi
# The AWS waiter can succeed after automatic rollback; verify the intended revision too.
if [[ "$current_task" != "$next_task" ]] \
  || ! readiness_status=$(curl --fail --silent --show-error --retry 5 --retry-delay 3 --retry-all-errors \
      --connect-timeout 5 --max-time 15 --output "$release_dir/readiness.json" --write-out '%{http_code}' \
      "${PUBLIC_BASE_URL%/}/actuator/health/readiness") \
  || [[ "$readiness_status" != 200 ]] \
  || ! jq -e 'type == "object" and .status == "UP"' "$release_dir/readiness.json" > /dev/null; then
  rollback
  exit 1
fi
echo "Deployment healthy: $next_task"
