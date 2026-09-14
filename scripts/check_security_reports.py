#!/usr/bin/env python3
"""Fail-closed release policy for completed Trivy artifact reports."""
import collections, datetime, hashlib, json, pathlib, sys
report_dir, jar, image, scan_failed = sys.argv[1:]
out = pathlib.Path(report_dir)
summary = {
    "scanned_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "image": image,
    "image_id": json.loads((out / "image-id.json").read_text()),
    "jar_sha256": hashlib.sha256(pathlib.Path(jar).read_bytes()).hexdigest(),
    "blocking_severities": ["HIGH", "CRITICAL"],
    "ignore_unfixed": False,
    "scan_errors": scan_failed != "0",
    "reports": {},
}
for name in ("dependencies", "image"):
    try:
        data = json.loads((out / f"{name}.json").read_text())
        results = data.get("Results", [])
        packages = sum(len(r.get("Packages", [])) for r in results)
        findings = [v for r in results for v in r.get("Vulnerabilities", [])]
        if name == "image":
            config = data.get("Metadata", {}).get("ImageConfig", {})
            summary["image_platform"] = f"{config.get('os', 'unknown')}/{config.get('architecture', 'unknown')}"
        # A missing dependency inventory is not a successful clean scan.
        if packages == 0:
            summary["scan_errors"] = True
        counts = collections.Counter(v.get("Severity", "UNKNOWN") for v in findings)
        summary["reports"][name] = {"packages": packages, "vulnerabilities_by_severity": dict(counts)}
    except (OSError, ValueError) as exc:
        summary["scan_errors"] = True
        summary["reports"][name] = {"error": str(exc)}
summary["passed"] = not summary["scan_errors"] and not any(
    counts.get(s, 0)
    for report in summary["reports"].values()
    for counts in [report.get("vulnerabilities_by_severity", {})]
    for s in summary["blocking_severities"]
)
(out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
sys.exit(0 if summary["passed"] else 1)
