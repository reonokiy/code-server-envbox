#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PYTHONDONTWRITEBYTECODE=1
python tests/envbox-integration.py
task_proto=$(mktemp -d)
trap 'rm -rf -- "$task_proto"' EXIT
curl -fsSL https://raw.githubusercontent.com/container-storage-interface/spec/v1.11.0/csi.proto \
  -o "$task_proto/csi.proto"
printf '%s  %s\n' 084208e2655661a752db4e3dd0ea7cbda2a538c886586a1b4115a56028a654dc \
  "$task_proto/csi.proto" | sha256sum -c -
python -m grpc_tools.protoc -I "$task_proto" --python_out="$task_proto" \
  --grpc_python_out="$task_proto" "$task_proto/csi.proto"
PYTHONPATH="$task_proto" python tests/s3-managed.py
