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
  echo "==> 错误: 未设置 HIPPO_DOCS_HOST 环境变量。"
  echo "    用法示例: HIPPO_DOCS_HOST=my-server HIPPO_TAILSCALE_IP=100.x.x.x ./scripts/deploy-docs-to-vps.sh"
  exit 1
fi

echo "==> [1/3] 构建 Hippo VitePress 文档站 (base: /hippo/) ..."
pnpm --dir "${DOCS_DIR}" run build

if [[ ! -d "${DIST_DIR}" ]]; then
  echo "==> 错误: ${DIST_DIR} 不存在，构建失败"
  exit 1
fi

echo "==> [2/3] 正在通过管道压缩并同步静态文档至 ${REMOTE_HOST}:${REMOTE_DIR} ..."
COPYFILE_DISABLE=1 tar --disable-copyfile -cz -C "${DIST_DIR}" . | ssh "${REMOTE_HOST}" "mkdir -p ${REMOTE_DIR} && tar --warning=no-unknown-keyword -xz -C ${REMOTE_DIR} && find ${REMOTE_DIR} -name '._*' -delete"

echo "==> [3/3] 部署完成！"
if [[ -n "${TS_IP}" || -n "${TS_DOMAIN}" ]]; then
  echo "==> 访问入口:"
  [[ -n "${TS_IP}" ]] && echo "    - Hippo 文档站 (IP):      http://${TS_IP}/hippo/"
  [[ -n "${TS_DOMAIN}" ]] && echo "    - Hippo 文档站 (Domain):  http://${TS_DOMAIN}/hippo/"
fi

