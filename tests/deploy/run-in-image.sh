#!/bin/sh
# Run the start-script tests inside the pinned Hermes image (it carries the python with ruamel.yaml).
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
IMG=$(sed -n 's/^FROM \(nousresearch[^ ]*\).*/\1/p' "$ROOT/deploy/railway/Dockerfile")
docker run --rm --network none --entrypoint sh -v "$ROOT:/src:ro" "$IMG" /src/tests/deploy/test_bh_start.sh

exec docker run --rm --network none --entrypoint /opt/hermes/.venv/bin/python -v "$ROOT:/src:ro" "$IMG" /src/tests/deploy/test_config_merge.py
