# Monitoring drill evidence

The [final run](final.json) passed on September 14, 2026 UTC using application image `sha256:c8a1bef9d5d0402c3b1546b7de27f7a305c214ab67517b20d1c187599ca7de2d`. It ran the real migration job, restricted application, PostgreSQL, Prometheus, Alertmanager, blackbox exporter, and a private disposable webhook receiver. Exact image IDs and sanitized configuration are in the report.

| Check | Observed result |
| --- | --- |
| Protected metrics | Anonymous HTTP 401; customer HTTP 403; authenticated Prometheus scrape `up=1` |
| JVM metrics | Eight memory series scraped |
| Committed business counters | Reservation created, replayed, and cancelled counters each equalled one |
| Database outage | Readiness probe HTTP 503 and `probe_success=0` |
| Application during outage | Metrics `up=1`, scrape age 1.682 seconds, liveness HTTP 200 |
| Alert evaluation | Prometheus reported `InventoryReadinessFailed` as firing |
| Actual firing notification | Private receiver receipt observed 17.016 seconds after database pause |
| Recovery | Readiness HTTP 200 and `probe_success=1` |
| Actual resolved notification | Same fingerprint as firing; observed 2.060 seconds after database unpause |
| Business state | Authenticated product read confirmed all nine units available |
| Cleanup | No errors; created containers, network, temporary storage, and credentials removed |
| Offline safeguards | 17 tests passed for destination restrictions, scrape guards, notification matching, and sanitized evidence |

The [successful pilot](pilot-before-scrape-guard.json) is retained separately. The final run additionally used explicit disabled redirects on the protected metrics scrape and the completed scrape-field/relabel allowlist. Both runs used the same application image; no application rebuild occurred between them.

All notifications stayed inside the private fixture. These observations do not establish production alert latency, Slack/email delivery, AWS behavior, durable monitoring retention, or high availability. See [the monitoring guide](../../docs/monitoring.md) for reproduction and limits.
