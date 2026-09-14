# Compare the current AWS snapshot to the one whose image was actually inspected.
$verified[0] as $baseline
| .failures == []
  and (.services | length) == 1
  and .services[0].status == "ACTIVE"
  and .services[0].taskDefinition == $baseline.previous_task
  and .services[0].desiredCount == $baseline.previous_count
  and .services[0].runningCount == $baseline.previous_running_count
  and .services[0].pendingCount == $baseline.previous_pending_count
  and .services[0].runningCount == .services[0].desiredCount
  and .services[0].pendingCount == 0
  and (.services[0].deployments | length) == 1
  and .services[0].deployments[0].id == $baseline.previous_deployment_id
  and .services[0].deployments[0].status == "PRIMARY"
  and .services[0].deployments[0].taskDefinition == $baseline.previous_task
  and .services[0].deployments[0].rolloutState == "COMPLETED"
  and .services[0].deployments[0].desiredCount == $baseline.previous_count
  and .services[0].deployments[0].runningCount == $baseline.previous_running_count
  and .services[0].deployments[0].pendingCount == 0
