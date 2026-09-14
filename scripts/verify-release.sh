#!/usr/bin/env bash
# Scan the packaged runtime dependencies and the exact local image to be published.
set -euo pipefail
cd "$(dirname "$0")/.."
image_ref=${1:-inventory-cloud-service-app:latest}
jar_file=${2:-target/inventory-cloud-service-0.0.1-SNAPSHOT.jar}
report_dir=${SECURITY_REPORT_DIR:-reports/security/latest}
trivy_version=0.74.0
trivy_dir=${TRIVY_INSTALL_DIR:-${TMPDIR:-/tmp}/inventory-trivy-$trivy_version}
trivy_cache=${TRIVY_CACHE_DIR:-${TMPDIR:-/tmp}/inventory-trivy-cache}
mkdir -p "$report_dir" "$trivy_dir" "$trivy_cache"
rm -f "$report_dir/dependencies.json" "$report_dir/image.json" "$report_dir/sbom.cdx.json" "$report_dir/summary.json"
[[ -f "$jar_file" ]] || { echo "Build the application JAR before scanning: $jar_file" >&2; exit 1; }

# Official GitHub release asset SHA256 values, verified 2026-09-13:
# https://github.com/aquasecurity/trivy/releases/tag/v0.74.0
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) asset=Linux-64bit; checksum=2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a ;;
  Darwin-arm64) asset=macOS-ARM64; checksum=1caada5e0e2091909357c7525d3aa76f4b660b13821bc143b190c7483e31cc11 ;;
  *) echo 'Supported scanner hosts: Linux x86_64 and macOS ARM64.' >&2; exit 1 ;;
esac
archive="$trivy_dir/trivy.tar.gz"
if [[ ! -f "$archive" ]]; then
  curl --fail --silent --show-error --location --retry 3 \
    "https://github.com/aquasecurity/trivy/releases/download/v$trivy_version/trivy_${trivy_version}_${asset}.tar.gz" \
    --output "$archive.partial"
  mv "$archive.partial" "$archive"
fi
if command -v sha256sum >/dev/null; then
  printf '%s  %s\n' "$checksum" "$archive" | sha256sum -c -
else
  printf '%s  %s\n' "$checksum" "$archive" | shasum -a 256 -c -
fi
tar -xzf "$archive" -C "$trivy_dir" trivy
trivy="$trivy_dir/trivy"

# Capture all severities. HIGH/CRITICAL (including unfixed findings) block release.
# Explicit empty ignore/config files prevent an ambient project file hiding findings.
scan_tmp=$(mktemp -d)
trap 'rm -rf "$scan_tmp"' EXIT
printf '{}\n' > "$scan_tmp/config.yaml"
: > "$scan_tmp/ignore"
cp "$jar_file" "$scan_tmp/application.jar"
image_id=$(docker image inspect "$image_ref" --format '{{.Id}}')
[[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Docker did not return a valid image ID.' >&2; exit 1; }
printf '"%s"\n' "$image_id" > "$report_dir/image-id.json"
scan_args=(--cache-dir "$trivy_cache" --config "$scan_tmp/config.yaml" --ignorefile "$scan_tmp/ignore" --timeout 15m
  --disable-telemetry --skip-version-check --no-progress --ignore-unfixed=false
  --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL --skip-db-update=false --skip-java-db-update=false)
scan_failed=0
# Rootfs mode enables the JAR archive analyzer; source-filesystem mode does not.
"$trivy" rootfs "${scan_args[@]}" --scanners vuln --pkg-types library --list-all-pkgs \
  --format json --output "$report_dir/dependencies.json" "$scan_tmp" \
  > "$report_dir/dependencies.log" 2>&1 || scan_failed=1
"$trivy" image "${scan_args[@]}" --image-src docker --scanners vuln --list-all-pkgs \
  --format json --output "$report_dir/image.json" "$image_id" \
  > "$report_dir/image.log" 2>&1 || scan_failed=1
if [[ -s "$report_dir/image.json" ]]; then
  "$trivy" convert --format cyclonedx --output "$report_dir/sbom.cdx.json" "$report_dir/image.json" \
    > "$report_dir/sbom.log" 2>&1 || scan_failed=1
else
  scan_failed=1
fi
"$trivy" --cache-dir "$trivy_cache" version --format json > "$report_dir/scanner.json"

python3 scripts/check_security_reports.py "$report_dir" "$scan_tmp/application.jar" "$image_ref" "$scan_failed"
