# Local image rollback and schema compatibility

`scripts/rollback-drill.py` demonstrates replacement of an unhealthy application image with the
previous verified image while keeping the same PostgreSQL database and loopback API address.
It also demonstrates that the previous application still works after an additive schema change.
This is a local Docker drill; it does not execute or establish AWS ECS rollback behavior.

## Run

Build the current application image, keep its local tag, and make PostgreSQL available:

```sh
docker build -t inventory-cloud-service-app .
docker pull postgres:17-bookworm
python3 scripts/rollback-drill.py --image inventory-cloud-service-app
```

Python's standard library and Docker are the only host requirements. The image must contain
the release contract required by `scripts/release_contract.py`. Both the original and derived
candidate images pass the same embedded capability validation used by the release tooling.
These capability checks do not constitute vulnerability scanning or release approval; run the
normal verification pipeline for the source image.

The script resolves the supplied image to an immutable ID before starting. `--image` may be an
image ID, provided the image also retains a local tag. `--database-image` and `--report` override
the supporting image and report location. The default report is `reports/rollback/latest.json`.
Only a successful drill with successful cleanup returns exit code zero.

## What actually happens

1. Create a uniquely named disposable network and PostgreSQL database. Run the source image's
   migration job with owner credentials, then use only `inventory_app` for the running API.
2. Start the verified image on a random loopback port. Create inventory and a reservation with
   an `Idempotency-Key`; record the returned records and successful migration history.
3. Copy the source image's application archive into a private temporary fixture directory. Add
   one SQL resource that creates a nullable `reservations.rollback_drill_note` column. Existing
   Java classes, dependencies, and migration entries are checked byte for byte and remain
   unchanged. No migration in the repository is created or modified.
4. Build a uniquely labelled local candidate image from that source. Relocate its Actuator base
   path, while its readiness probe retains the original path. This makes the candidate
   deliberately unhealthy without changing business code. The private build context contains
   only the fixture Dockerfile and candidate archive; it never becomes the repository's build
   context or a published image.
5. Run the candidate's real migration entry point. Confirm one successful additional migration,
   unchanged previous migration checksums, and the new nullable column. Check that the previous
   application can still read the original records on the upgraded schema.
6. Stop the original API and start the candidate on the same loopback address. Require that its
   authenticated business endpoint works, its configured readiness endpoint returns HTTP 404,
   and Docker records an unhealthy state with an actual failed health command.
7. Stop the candidate and start the original immutable image again on that same address. Confirm
   readiness, inspect the restored container's exact image ID, and measure the recovery interval.
8. Verify all original product and reservation fields, replay the same reservation key without
   consuming more stock, and cancel twice. Replaying the cancelled request must still return
   the original reservation. Confirm the added column and migration history remain intact.

The report records the source, candidate, and restored image IDs; contract validation; migration
versions, descriptions, and checksums; observed readiness failure; record preservation; and two
timings. One timing covers failure detection to restored readiness. The other includes the
entire interval from stopping the original API through candidate failure and restoration.

## Cleanup and interpretation

The existing Compose application, its volume, cloud resources, and saved credentials are not
used. The fixture generates its own passwords and uses memory-backed PostgreSQL storage. It
tracks exact container, network, and candidate image identities. Removal requires matching
ownership labels, and candidate cleanup cannot remove the verified source image.

BuildKit needs a temporary local tag for the immutable source ID. The script creates a unique
fixture tag, removes only that tag, and refuses to remove the source image's last tag. It checks
that the verified source image still exists afterward. The generated candidate image and private
temporary archives are removed after the drill. Cleanup failures make the report fail.

This proves compatibility for one nullable-column addition and this application's current API
and idempotency behavior. It does not prove arbitrary schema changes are safe to roll back.
Removing or renaming required columns, incompatible data transformations, and other destructive
migrations still need an explicit compatibility and recovery plan. Image rollback deliberately
does not reverse database migrations.

The existing offline deployment tests exercise ECS command sequencing and failure handling with
simulated AWS calls. A separate disposable cloud drill is still needed to verify ECS scheduling,
load balancer traffic switching, the deployment circuit breaker, and cloud recovery timing.

The recorded local run on September 14, 2026 passed all six runtime checks and restored image
`sha256:c8a1bef9d5d0402c3b1546b7de27f7a305c214ab67517b20d1c187599ca7de2d` in **12.78 seconds** after
candidate failure detection. The full original-stop-to-restored-readiness interval was **25.60
seconds**. Cleanup removed the candidate image and temporary tag with no errors. These are
observations from this local fixture, not cloud recovery objectives. The exact configuration and
migration history are in [the recorded report](../reports/rollback/latest.json).
