#!/usr/bin/env bash
# One-process Linux release deployment for radar-sim.
#
# The unified serve-v1 process provides the Web console, REST/SDK API,
# scheduler, authenticated Windows Agent endpoints and the Cluster executor.
# Linux never compiles Selena.
#
# Usage:
#   bash scripts/linux_deploy.sh --yes
#   bash scripts/linux_deploy.sh status|test|stop
#   bash scripts/linux_deploy.sh credentials  # explicitly reveal onboarding tokens
#
# Optional environment overrides:
#   RSIM_INSTALL_DIR, RSIM_HOME, RSIM_PORT, RSIM_OWNER, RSIM_AGENT_ID,
#   RSIM_REPO_URL, RSIM_AUTH_FILE, RSIM_INSECURE_NO_AUTH

set -eu

REPO_URL="${RSIM_REPO_URL:-https://github.com/Bobs1121/radar-sim.git}"
INSTALL_DIR="${RSIM_INSTALL_DIR:-${HOME}/radar-sim}"
DATA_DIR="${RSIM_HOME:-${HOME}/rsim_data}"
PORT="${RSIM_PORT:-8878}"
OWNER="${RSIM_OWNER:-admin}"
AGENT_ID="${RSIM_AGENT_ID:-windows-agent}"
AUTH_FILE="${RSIM_AUTH_FILE:-${DATA_DIR}/http-auth.json}"
# Current product sprint intentionally has no user login.  Set to 0 when the
# authenticated pairing sprint is released; never expose this mode to an
# untrusted network.
INSECURE_NO_AUTH="${RSIM_INSECURE_NO_AUTH:-1}"
PID_FILE="${DATA_DIR}/serve-v1.pid"
LOG_FILE="${DATA_DIR}/serve-v1.log"
VENV_DIR="${INSTALL_DIR}/.venv"
VENV_PY="${VENV_DIR}/bin/python"

c_red()    { printf "\033[31m%s\033[0m\n" "$1"; }
c_green()  { printf "\033[32m%s\033[0m\n" "$1"; }
c_yellow() { printf "\033[33m%s\033[0m\n" "$1"; }
c_cyan()   { printf "\033[36m%s\033[0m\n" "$1"; }
die() { c_red "ERROR: $1" >&2; exit 1; }

preflight() {
    command -v python3 >/dev/null 2>&1 || die "python3 未安装（serve-v1 需要 Python 3.10+）"
    python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
        || die "Python 版本过低（serve-v1 需要 Python 3.10+）"
    command -v git >/dev/null 2>&1 || die "git 未安装"
    command -v curl >/dev/null 2>&1 || die "curl 未安装"
    c_green "Python $(python3 -c 'import platform; print(platform.python_version())') / git / curl OK"
}

fetch_code() {
    if [ -d "${INSTALL_DIR}/.git" ]; then
        c_cyan "==> 检查已有安装目录 ${INSTALL_DIR}"
        if [ -n "$(git -C "$INSTALL_DIR" status --porcelain)" ]; then
            c_yellow "安装目录有未提交修改；为避免覆盖，跳过自动更新并使用当前代码。"
            return
        fi
        git -C "$INSTALL_DIR" fetch --quiet origin main || die "git fetch 失败"
        git -C "$INSTALL_DIR" merge --ff-only origin/main >/dev/null \
            || die "代码无法 fast-forward；请人工处理分支后重试"
        c_green "代码已 fast-forward 到 origin/main"
    else
        c_cyan "==> 克隆代码到 ${INSTALL_DIR}"
        git clone --depth 1 "$REPO_URL" "$INSTALL_DIR" || die "git clone 失败"
    fi
}

install_runtime() {
    c_cyan "==> 安装统一 Web/API/调度控制面"
    if [ ! -x "$VENV_PY" ]; then
        python3 -m venv "$VENV_DIR" || die "创建 venv 失败"
    fi
    "$VENV_PY" -m pip install --quiet --upgrade pip
    (
        cd "$INSTALL_DIR"
        "$VENV_PY" -m pip install --quiet -e ".[v5-server]"
    ) || die "安装 serve-v1 依赖失败"
    "$VENV_PY" "$INSTALL_DIR/scripts/build_windows_connector_bundle.py" \
        --out "$INSTALL_DIR/dist/rsim-windows-connector.zip" \
        || die "构建 Windows 一键连接包失败"
    c_green "serve-v1 运行环境就绪"
}

ensure_auth() {
    mkdir -p "$DATA_DIR"
    if [ "$INSECURE_NO_AUTH" = "1" ]; then
        c_yellow "当前 Sprint 未启用登录；Windows 一键连接不保存令牌。仅限受信内网使用。"
        return
    fi
    if [ -f "$AUTH_FILE" ]; then
        "$VENV_PY" -c 'from core.http_auth import load_http_auth; import sys; load_http_auth(sys.argv[1])' "$AUTH_FILE" \
            || die "认证文件无效：${AUTH_FILE}"
        c_green "复用已有认证配置 ${AUTH_FILE}"
        return
    fi
    "$VENV_PY" - "$AUTH_FILE" "$OWNER" "$AGENT_ID" <<'PY'
import sys
from core.http_auth import create_http_auth_config
create_http_auth_config(sys.argv[1], users=[sys.argv[2]], agents={sys.argv[3]: sys.argv[2]})
PY
    chmod 600 "$AUTH_FILE"
    c_green "已创建认证配置 ${AUTH_FILE}（仅当前用户可读）"
}

start_server() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        c_yellow "serve-v1 已运行 (PID $(cat "$PID_FILE"))"
        return
    fi
    mkdir -p "$DATA_DIR/results"
    c_cyan "==> 启动统一控制面 :${PORT}"
    (
        cd "$INSTALL_DIR"
        if [ "$INSECURE_NO_AUTH" = "1" ]; then
            RSIM_HOME="$DATA_DIR" nohup "$VENV_PY" rsim.py server serve-v1 \
                --host 0.0.0.0 --port "$PORT" --insecure-no-auth \
                > "$LOG_FILE" 2>&1 &
        else
            RSIM_HOME="$DATA_DIR" nohup "$VENV_PY" rsim.py server serve-v1 \
                --host 0.0.0.0 --port "$PORT" --auth-file "$AUTH_FILE" \
                > "$LOG_FILE" 2>&1 &
        fi
        echo $! > "$PID_FILE"
    )
    i=0
    while [ "$i" -lt 20 ]; do
        if curl -fsS "http://127.0.0.1:${PORT}/api/v1/health" >/dev/null 2>&1; then
            c_green "serve-v1 启动成功 (PID $(cat "$PID_FILE"))"
            return
        fi
        i=$((i + 1))
        sleep 1
    done
    tail -20 "$LOG_FILE" 2>/dev/null || true
    die "serve-v1 启动失败，日志：${LOG_FILE}"
}

stop_server() {
    if [ ! -f "$PID_FILE" ]; then
        c_yellow "serve-v1 未运行"
        return
    fi
    pid=$(cat "$PID_FILE")
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid"
        c_green "已停止 serve-v1 (PID $pid)"
    else
        c_yellow "PID $pid 已不存在"
    fi
    rm -f "$PID_FILE"
}

show_status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        c_green "serve-v1: 运行中 (PID $(cat "$PID_FILE"), port ${PORT})"
        curl -fsS "http://127.0.0.1:${PORT}/api/v1/health" && printf '\n'
    else
        c_red "serve-v1: 未运行"
        return 1
    fi
    c_cyan "Web/API: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT}/"
}

selftest() {
    curl -fsS "http://127.0.0.1:${PORT}/api/v1/health" >/dev/null \
        || die "health 失败"
    if [ "$INSECURE_NO_AUTH" = "1" ]; then
        curl -fsS "http://127.0.0.1:${PORT}/api/v1/capabilities" >/dev/null \
            || die "统一 v1 API 自检失败"
        c_green "health + 无登录模式 + 统一 v1 API 自检通过"
        return
    fi
    token=$("$VENV_PY" - "$AUTH_FILE" "$OWNER" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
owner = sys.argv[2] if sys.argv[2] in d["users"] else next(iter(d["users"]))
print(d["users"][owner])
PY
)
    code=$(curl -sS -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer ${token}" \
        "http://127.0.0.1:${PORT}/api/v1/runtime-bundles")
    [ "$code" = "200" ] || die "授权 API 自检失败 (HTTP ${code})"
    c_green "health + Bearer 身份 + 统一 v1 API 自检通过"
}

show_credentials() {
    [ "$INSECURE_NO_AUTH" != "1" ] || die "当前 Sprint 登录已关闭，没有用户令牌或 Agent 令牌"
    [ -f "$AUTH_FILE" ] || die "认证文件不存在；请先执行部署"
    c_yellow "以下是敏感凭证，仅在受信终端使用；不要粘贴到工单或日志。"
    "$VENV_PY" - "$AUTH_FILE" "$OWNER" "$AGENT_ID" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
owner, agent = sys.argv[2], sys.argv[3]
if owner not in d["users"] and len(d["users"]) == 1:
    owner = next(iter(d["users"]))
if agent not in d["agents"] and len(d["agents"]) == 1:
    agent = next(iter(d["agents"]))
print(f"RSIM owner:       {owner}")
print(f"RSIM_API_TOKEN:   {d['users'][owner]}")
print(f"Agent id:         {agent}")
print(f"RSIM_AGENT_TOKEN: {d['agents'][agent]['token']}")
PY
}

main() {
    action="${1:-start}"
    case "$action" in
        stop) stop_server; exit 0 ;;
        status) show_status; exit $? ;;
        test) selftest; exit 0 ;;
        credentials) show_credentials; exit 0 ;;
        start|--yes) ;;
        *) echo "用法: $0 [start|--yes|stop|status|test|credentials]"; exit 1 ;;
    esac

    c_cyan "========== radar-sim Linux unified control plane =========="
    preflight
    fetch_code
    install_runtime
    ensure_auth
    if [ "$action" != "--yes" ]; then
        printf "\n将启动 Web + API + scheduler + Cluster executor，Linux 不编译 Selena。\n"
        printf "安装目录: %s\n数据目录: %s\n端口: %s\n" "$INSTALL_DIR" "$DATA_DIR" "$PORT"
        read -r -p "继续？[y/N] " answer
        [ "$answer" = "y" ] || [ "$answer" = "Y" ] || exit 0
    fi
    start_server
    selftest
    printf '\n'
    show_status
    c_green "部署完成。Web 与 SDK 使用同一个 serve-v1 入口。"
    if [ "$INSECURE_NO_AUTH" = "1" ]; then
        printf "Windows 一键连接: Web 页面下载后双击运行（无需令牌）\n"
    else
        printf "Windows Agent 凭证（显式查看）: bash scripts/linux_deploy.sh credentials\n"
    fi
    printf "日志: %s\n" "$LOG_FILE"
}

main "$@"
