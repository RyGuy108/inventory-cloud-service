#!/usr/bin/env bash
set -euo pipefail
service_url=${1:-http://localhost:8080}
for probe in liveness readiness; do
  curl --fail --silent --show-error --max-time 15 "${service_url%/}/actuator/health/$probe"
  echo
done
