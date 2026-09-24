#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS_DIR="${ROOT_DIR}/docs"
DIST_DIR="${DOCS_DIR}/.vitepress/dist"
REMOTE_HOST="${HIPPO_DOCS_HOST:-}"
REMOTE_DIR="${HIPPO_DOCS_DIR:-/opt/docs/hippo}"
TS_IP="${HIPPO_TAILSCALE_IP:-}"
TS_DOMAIN="${HIPPO_TAILSCALE_DOMAIN:-}"

if [[ -z "${REMOTE_HOST}" ]]; then
  echo "==> Error: HIPPO_DOCS_HOST environment variable is not set."
  echo "    Usage example: HIPPO_DOCS_HOST=my-server HIPPO_TAILSCALE_IP=100.x.x.x ./scripts/deploy-docs-to-vps.sh"
  exit 1
fi

echo "==> [1/3] Building Hippo VitePress documentation site (base: /hippo/) ..."
pnpm --dir "${DOCS_DIR}" run build

if [[ ! -d "${DIST_DIR}" ]]; then
  echo "==> Error: ${DIST_DIR} does not exist, build failed"
  exit 1
fi

echo "==> [2/3] Compressing and syncing static docs via pipe to ${REMOTE_HOST}:${REMOTE_DIR} ..."
COPYFILE_DISABLE=1 tar --disable-copyfile -cz -C "${DIST_DIR}" . | ssh "${REMOTE_HOST}" "mkdir -p ${REMOTE_DIR} && tar --warning=no-unknown-keyword -xz -C ${REMOTE_DIR} && find ${REMOTE_DIR} -name '._*' -delete"

echo "==> [3/3] Deployment complete!"
if [[ -n "${TS_IP}" || -n "${TS_DOMAIN}" ]]; then
  echo "==> Access URLs:"
  [[ -n "${TS_IP}" ]] && echo "    - Hippo Docs (IP):      http://${TS_IP}/hippo/"
  [[ -n "${TS_DOMAIN}" ]] && echo "    - Hippo Docs (Domain):  http://${TS_DOMAIN}/hippo/"
fi

