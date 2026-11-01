#!/usr/bin/env bash
# Start or stop the lab backend, which serves the built UI and the API on one port.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
cd "$root"

app=backend.app:create_app
action=
host=127.0.0.1
port=8000
fresh=0
stop_timeout=${STOP_TIMEOUT:-10}

usage() {
  cat <<'EOF'
用法：./start.sh [start|stop] [new|--new] [--host 地址] [--port 端口]

  start, --start  （默认，可省略）停止占用该端口的遗留后端进程，按需构建前端，然后启动服务
  stop, --stop    停止该端口上本项目的后端服务后退出；没有运行中的服务时直接成功返回
  new, --new      启动前先清空 SQLite 数据库（实验历史会丢失，无法恢复），全新启动；不能与 stop 同用
  --host          监听地址，默认 127.0.0.1（stop 时忽略）
  --port          监听端口，默认 8000；stop 时需与启动时一致
  -h, --help      显示本帮助

只会停止命令行含 backend.app:create_app、工作目录为本项目且正在监听该端口的进程；
端口被其他程序占用时不会强杀：start 报错退出，stop 只提示。
环境变量 STOP_TIMEOUT：等待旧进程退出的秒数（默认 10），超时后强制结束。
EOF
}

log() { printf '[start] %s\n' "$*"; }
warn() { printf '[start] 警告：%s\n' "$*" >&2; }
die() { printf '[start] 错误：%s\n' "$*" >&2; exit 1; }
usage_error() { printf '[start] 错误：%s\n\n' "$1" >&2; usage >&2; exit 2; }

while (($#)); do
  case $1 in
    start | --start | stop | --stop)
      [[ -z $action || $action == "${1#--}" ]] || usage_error "start 与 stop 只能指定一个"
      action=${1#--}
      ;;
    new | --new) fresh=1 ;;
    --host | --port)
      (($# >= 2)) || usage_error "$1 需要一个参数"
      if [[ $1 == --host ]]; then host=$2; else port=$2; fi
      shift
      ;;
    --host=*) host=${1#*=} ;;
    --port=*) port=${1#*=} ;;
    -h | --help | help) usage; exit 0 ;;
    *) usage_error "未知参数：$1" ;;
  esac
  shift
done

action=${action:-start}
if [[ $action == stop ]] && ((fresh)); then usage_error "stop 不能与 new 同时使用"; fi
if ! [[ $port =~ ^[0-9]{1,5}$ ]] || ((10#$port < 1 || 10#$port > 65535)); then
  usage_error "端口无效：$port"
fi
port=$((10#$port))
[[ $host =~ ^[A-Za-z0-9:][A-Za-z0-9._:-]*$ ]] || usage_error "监听地址无效：$host"
[[ $stop_timeout =~ ^[0-9]{1,4}$ ]] || usage_error "STOP_TIMEOUT 必须是整数秒"

# Resolve DATABASE_PATH exactly like the app does (environment, then .env, then default).
database_path() {
  local path
  path=$(uv run --frozen python -c 'from backend.models import Settings; print(Settings().database_path)' |
    tail -n 1) || die "读取配置失败，已取消（见上方错误）"
  [[ -n $path ]] || die "DATABASE_PATH 为空，无法确定要清空的数据库"
  [[ $path == /* ]] || path=$root/$path
  [[ ! -d $path ]] || die "DATABASE_PATH 指向目录，已取消：$path"
  printf '%s\n' "$path"
}

wipe_database() {
  local file removed=0
  for file in "$database" "$database-wal" "$database-shm" "$database-journal"; do
    if [[ -e $file || -L $file ]]; then
      rm -f -- "$file"
      log "已删除 $file"
      removed=1
    fi
  done
  ((removed)) || log "数据库文件不存在，无需清空：$database"
}

# The backend serves frontend/dist directly, so make sure it exists and is current.
ensure_frontend() {
  local index=frontend/dist/index.html newer
  if [[ -f $index ]]; then
    newer=$(find frontend/src frontend/scripts frontend/index.html frontend/package.json \
      frontend/package-lock.json frontend/vite.config.ts frontend/tsconfig.json \
      -newer "$index" 2>/dev/null | head -n 1 || true)
    [[ -n $newer ]] || return 0
    log "前端源码已更新（$newer），重新构建…"
  else
    log "未找到 frontend/dist，开始构建前端…"
  fi
  if ! command -v npm >/dev/null 2>&1; then
    warn "未找到 npm，跳过前端构建：UI 不可用或不是最新（API 不受影响）"
    return 0
  fi
  [[ -d frontend/node_modules ]] || npm ci --prefix frontend || die "npm ci 失败，已取消启动"
  npm run build --prefix frontend || die "前端构建失败，已取消启动（正在运行的旧进程未受影响）"
}

# One PID per listener on $port; "unknown" when the owner is hidden (e.g. another user).
listeners() {
  local line
  if command -v ss >/dev/null 2>&1; then
    { ss -ltnp "sport = :$port" 2>/dev/null || true; } | tail -n +2 | while IFS= read -r line; do
      if [[ $line == *pid=* ]]; then
        grep -o 'pid=[0-9]*' <<<"$line" | cut -d= -f2
      else
        echo unknown
      fi
    done | sort -u
  elif command -v lsof >/dev/null 2>&1; then
    { lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true; } | sort -u
  fi
}

# Only this project's backend counts as a leftover; /proc is absent on macOS, so skip the cwd check there.
is_lab_server() {
  local pid=$1 cwd
  [[ $(ps -ww -o args= -p "$pid" 2>/dev/null) == *"$app"* ]] || return 1
  cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
  [[ -z $cwd || $cwd == "$root" ]]
}

# Zombies hold no sockets, so they count as exited.
alive() {
  local state
  state=$(ps -o stat= -p "$1" 2>/dev/null) || return 1
  state=${state//[[:space:]]/}
  [[ -n $state && $state != Z* ]]
}

any_alive() {
  local pid
  for pid in "$@"; do
    if alive "$pid"; then return 0; fi
  done
  return 1
}

wait_exit() {
  local deadline=$((SECONDS + $1))
  shift
  while any_alive "$@"; do
    ((SECONDS < deadline)) || return 1
    sleep 0.2
  done
}

# Stop this project's backend listening on $port; returns 1 when there is none.
stop_lab_server() {
  local label=$1 pid deadline
  local -a mine=()
  for pid in $(listeners); do
    if [[ $pid != unknown ]] && is_lab_server "$pid"; then mine+=("$pid"); fi
  done
  ((${#mine[@]})) || return 1

  log "正在停止端口 $port 上的${label}（PID ${mine[*]}）…"
  kill -TERM "${mine[@]}" 2>/dev/null || true
  if ! wait_exit "$stop_timeout" "${mine[@]}"; then
    warn "$stop_timeout 秒内未退出，强制结束"
    kill -KILL "${mine[@]}" 2>/dev/null || true
    wait_exit 5 "${mine[@]}" || die "无法结束进程 ${mine[*]}"
  fi
  deadline=$((SECONDS + 5))
  while [[ -n $(listeners) ]] && ((SECONDS < deadline)); do sleep 0.2; done
  log "${label}已停止"
}

# Report whatever still listens on $port; returns 1 when the port is free.
report_other_listeners() {
  local pid left
  left=$(listeners)
  [[ -n $left ]] || return 1
  warn "端口 $port 已被其他程序占用，未做处理："
  for pid in $left; do
    if [[ $pid == unknown ]]; then
      warn "  无法识别占用进程（可能属于其他用户）"
    else
      warn "  PID $pid：$(ps -ww -o args= -p "$pid" 2>/dev/null | cut -c1-160)"
    fi
  done
}

free_port() {
  stop_lab_server 遗留后端进程 || true
  if report_other_listeners; then die "请先释放该端口，或用 --port 指定其他端口"; fi
}

has_port_tools() { command -v ss >/dev/null 2>&1 || command -v lsof >/dev/null 2>&1; }

if [[ $action == stop ]]; then
  has_port_tools || die "未找到 ss 或 lsof，无法定位要停止的服务"
  stop_lab_server 后端服务 || log "端口 $port 上没有本项目运行中的后端服务，无需停止"
  report_other_listeners || true
  exit 0
fi

command -v uv >/dev/null 2>&1 || die "未找到 uv，请先安装：https://docs.astral.sh/uv/"
has_port_tools || warn "未找到 ss 或 lsof，无法检测遗留进程"
[[ -f .env ]] || warn "没有 .env，将使用默认配置；可参考 .env.example 创建"

# Fail early on bad config or build errors, and do the destructive steps last.
database=
if ((fresh)); then database=$(database_path); fi
ensure_frontend
free_port
if ((fresh)); then wipe_database; fi

log "启动服务：http://$host:$port（Ctrl+C 停止）"
exec uv run --frozen uvicorn "$app" --factory --host "$host" --port "$port"
