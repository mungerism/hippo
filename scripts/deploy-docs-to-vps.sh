#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS_DIR="${ROOT_DIR}/docs"
DIST_DIR="${DOCS_DIR}/.vitepress/dist"
REMOTE_HOST="${HIPPO_DOCS_HOST:-dedirock-us-lax-public}"
REMOTE_DIR="/opt/docs/hippo"

echo "==> [1/3] 构建 Hippo VitePress 文档站 (base: /hippo/) ..."
pnpm --dir "${DOCS_DIR}" run build

if [[ ! -d "${DIST_DIR}" ]]; then
  echo "==> 错误: ${DIST_DIR} 不存在，构建失败"
  exit 1
fi

echo "==> [2/3] 正在通过管道压缩并同步静态文档至 ${REMOTE_HOST}:${REMOTE_DIR} ..."
COPYFILE_DISABLE=1 tar --disable-copyfile -cz -C "${DIST_DIR}" . | ssh "${REMOTE_HOST}" "mkdir -p ${REMOTE_DIR} && tar --warning=no-unknown-keyword -xz -C ${REMOTE_DIR} && find ${REMOTE_DIR} -name '._*' -delete"

echo "==> [3/3] 部署完成！"
echo "==> 手机/设备访问入口 (Tailscale 私网，零公网暴露):"
echo "    - Hippo 文档站:  http://100.81.230.23/hippo/  或  http://dmit-lax-as3.perch-rainbow.ts.net/hippo/"
echo "    - 统一文档大厅:  http://100.81.230.23/        或  http://dmit-lax-as3.perch-rainbow.ts.net/"
