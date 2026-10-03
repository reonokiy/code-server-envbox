#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
revision=$(git rev-parse HEAD)
if [[ $revision != "$(git rev-parse origin/main)" || -n $(git status --porcelain) ]]; then
  echo 'Publication requires a clean reviewed origin/main checkout.' >&2
  exit 1
fi
export AWS_PROFILE=reonokiy
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
uri=$(aws --region us-east-1 ecr-public describe-repositories \
  --repository-names talos/code-server --query 'repositories[0].repositoryUri' --output text)
if [[ $uri != public.ecr.aws/a1y5s4n2/talos/code-server && $uri != ecr-public.aws.com/a1y5s4n2/talos/code-server ]]; then
  echo 'Unexpected public ECR repository; refusing publication.' >&2
  exit 1
fi
for component in outer workspace tailnet; do
  tag="envbox-$component-$revision"
  existing=$(aws --region us-east-1 ecr-public describe-images --repository-name talos/code-server \
    --query "imageDetails[?imageTags && contains(imageTags, '$tag')].imageDigest | [0]" --output text)
  if [[ $existing != None ]]; then
    echo 'Revision tag already exists; refusing overwrite.' >&2
    exit 1
  fi
done
task_docker=$(mktemp -d)
trap 'rm -rf -- "$task_docker"' EXIT
# Only helper names reach disk. SSO-derived registry credentials stay in memory.
printf '%s\n' '{"credHelpers":{"public.ecr.aws":"ecr-login","ecr-public.aws.com":"ecr-login"}}' > "$task_docker/config.json"
export DOCKER_CONFIG="$task_docker"
export AWS_ECR_DISABLE_CACHE=true
export AWS_ECR_CACHE_DIR="$task_docker/ecr"
bash scripts/build.sh
uv run --with-requirements tests/requirements.txt bash scripts/test.sh
if [[ $(git rev-parse HEAD) != "$revision" || -n $(git status --porcelain) ]]; then
  echo 'Source changed during verification; refusing publication.' >&2
  exit 1
fi
for component in outer workspace tailnet; do
  case "$component" in
    outer) local_image=code-server-envbox:test ;;
    workspace) local_image=code-server-workspace:test ;;
    tailnet) local_image=code-server-relay:test ;;
  esac
  image="$uri:envbox-$component-$revision"
  docker tag "$local_image" "$image"
  docker push "$image"
  docker image inspect "$image" --format '{{join .RepoDigests "\n"}}'
done
