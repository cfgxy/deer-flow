#!/usr/bin/env bash
# DeerFlow 本地源码部署运维入口（RUYI-117）
#
# 用途：对 /home/guxy/srv/deerflow 这套「源码检出 + Docker Compose 生产栈」形态的
# 本地部署做可重复的启停、重启、健康检查、日志归档与卸载。
#
# 设计约束：
#   - 只包装仓库既有入口（scripts/deploy.sh / docker compose），不新增部署逻辑；
#   - 只操作 docker compose 项目 deer-flow 名下的容器与卷，不触碰其它容器；
#   - 不接收、不打印任何凭据；凭据一律由部署目录下 gitignored 的 .env 注入。
#
# 用法：
#   scripts/deerflow-local-ops.sh <command>
#
#   up        构建镜像并启动（首次部署 / 源码基线变更后使用）
#   start     不重建，直接启动已有镜像（日常启动、重启恢复）
#   down      停止并移除本部署的容器（保留数据卷与数据库文件）
#   restart   down + start
#   status    容器状态、端口映射、资源占用
#   health    健康探针（/health 与 /health/ready）
#   logs      跟随查看网关日志（Ctrl-C 退出）
#   uninstall 完整卸载：移除容器、数据卷、本部署构建的镜像（需二次确认）
#
# 环境变量：
#   DEERFLOW_DEPLOY_DIR  部署目录，默认 /home/guxy/srv/deerflow
#   DEERFLOW_URL         对外入口，默认 http://127.0.0.1:2026

set -euo pipefail

DEPLOY_DIR="${DEERFLOW_DEPLOY_DIR:-/home/guxy/srv/deerflow}"
URL="${DEERFLOW_URL:-http://127.0.0.1:2026}"
COMPOSE_FILE="docker/docker-compose.yaml"
PROJECT="deer-flow"
LOG_DIR="${DEPLOY_DIR}/.local-ops/logs"

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
info() { printf '\033[0;34m==> %s\033[0m\n' "$*"; }

[ -d "$DEPLOY_DIR" ] || die "部署目录不存在：$DEPLOY_DIR"
[ -f "${DEPLOY_DIR}/config.yaml" ] || die "缺少 ${DEPLOY_DIR}/config.yaml（由 config.example.yaml 复制后按需修改，gitignored）"
[ -f "${DEPLOY_DIR}/.env" ] || die "缺少 ${DEPLOY_DIR}/.env（由 .env.example 复制后注入本机凭据，gitignored）"
mkdir -p "$LOG_DIR"

cd "$DEPLOY_DIR"

# 说明：docker-compose.yaml 依赖 scripts/deploy.sh 在运行时导出的一批变量
# （DEER_FLOW_HOME / DEER_FLOW_CONFIG_PATH / BETTER_AUTH_SECRET 等，部分来自
# backend/.deer-flow 下的密钥文件）。直接调 `docker compose` 会因变量为空而解析失败，
# 因此所有 compose 生命周期操作一律走 deploy.sh；只读查询用 docker 原生命令按容器名过滤。
containers() { docker ps -a --filter "name=${PROJECT}-" --format "$1"; }

# 归档一次带时间戳的部署日志，便于事后追溯本次操作的原始输出
run_logged() {
  local tag="$1"; shift
  local log="${LOG_DIR}/${tag}-$(date +%Y%m%d-%H%M%S).log"
  info "输出归档：$log"
  set +e
  "$@" 2>&1 | tee "$log"
  local rc=${PIPESTATUS[0]}
  set -e
  return "$rc"
}

cmd_up() { run_logged "build-up" bash ./scripts/deploy.sh; }
cmd_start() { run_logged "start" bash ./scripts/deploy.sh start; }
cmd_down() { run_logged "down" bash ./scripts/deploy.sh down; }

cmd_restart() {
  cmd_down
  cmd_start
}

cmd_status() {
  info "容器状态"
  printf '%-24s %-10s %-28s %s\n' NAME STATE STATUS PORTS
  containers '{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Ports}}' \
    | awk -F'\t' '{printf "%-24s %-10s %-28s %s\n", $1, $2, $3, $4}'
  info "资源占用"
  local names
  names="$(docker ps --filter "name=${PROJECT}-" --format '{{.Names}}' | tr '\n' ' ')"
  if [ -n "${names// /}" ]; then docker stats --no-stream $names; else echo "（无运行中容器）"; fi
  info "数据与卷"
  docker volume ls --filter "name=${PROJECT}_"
  du -sh "${DEPLOY_DIR}/backend/.deer-flow" 2>/dev/null || true
}

cmd_health() {
  local rc=0
  for p in /health /health/ready; do
    printf '%s -> ' "$p"
    curl -fsS --max-time 10 "${URL}${p}" || { rc=1; printf '(失败)'; }
    printf '\n'
  done
  return "$rc"
}

cmd_logs() { docker logs -f --tail 200 "${PROJECT}-gateway"; }

cmd_uninstall() {
  cat <<EOF
即将完整卸载本地 DeerFlow 部署：
  1. 移除 compose 项目 ${PROJECT} 的全部容器
  2. 删除数据卷 ${PROJECT}_redis-data（Redis 数据不可恢复）
  3. 删除本部署构建的镜像 deer-flow-gateway / deer-flow-frontend

不会删除：${DEPLOY_DIR} 目录本身、SQLite 数据库文件、config.yaml 与 .env。
如需彻底清空，请在本命令完成后手动执行：
  git worktree remove --force ${DEPLOY_DIR}

EOF
  read -r -p "确认执行？输入 yes 继续：" ans
  [ "$ans" = "yes" ] || die "已取消"
  cmd_down || true
  docker volume rm "${PROJECT}_redis-data" || true
  docker rmi deer-flow-gateway:latest deer-flow-frontend:latest || true
  info "卸载完成。数据库文件仍保留在 ${DEPLOY_DIR}/backend/.deer-flow/data"
}

case "${1:-}" in
  up) cmd_up ;;
  start) cmd_start ;;
  down) cmd_down ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  health) cmd_health ;;
  logs) cmd_logs ;;
  uninstall) cmd_uninstall ;;
  *)
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
