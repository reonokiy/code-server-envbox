#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
docker build -f Dockerfile.outer -t code-server-envbox:test .
docker build -f Dockerfile.workspace -t code-server-workspace:test .
docker build -f Dockerfile.tailnet -t code-server-relay:test .
# Test clients use the same public, pinned upstream base as both workspace images.
base=$(awk '$1 == "FROM" {print $2; exit}' Dockerfile.tailnet)
docker pull "$base"
docker tag "$base" code-server-tailnet:test
