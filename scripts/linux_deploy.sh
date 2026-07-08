#!/usr/bin/env bash
# radar-sim Linux 部署脚本（模式 A：cluster-only 服务）
#
# 在 Linux 上部署：
#   - 后端 control server（端口 8877）：HTTP API，--allowed-task-types cluster.run
#   - 前端 web 控制台（端口 8765）：浏览器 UI，转发任务到 8877
#
# 用法：
#   bash scripts/linux_deploy.sh           # 交互式（提示确认）
#   bash scripts/linux_deploy.sh --yes     # 跳过确认
#   bash scripts/linux_deploy.sh stop      # 停止服务
#   bash scripts/linux_deploy.sh status    # 查看状态
#
# 前置：Linux 装了 Python 3.9+ 和 git。无需 pip（control server 纯 stdlib）；
#       web 前端需要 PyYAML（脚本会尝试装）。

set -u

REPO_URL="https://github.com/Bobs1121/radar-sim.git"
INSTALL_DIR="${HOME}/radar-sim"
DATA_DIR="${HOME}/rsim_data"
SERVER_PORT=8877
WEB_PORT=8765
PROJECT="${RSIM_PROJECT:-ovrs25}"
SERVER_PID_FILE="${INSTALL_DIR}/.rsim_server.pid"
WEB_PID_FILE="${INSTALL_DIR}/.rsim_web.pid"
SERVER_LOG="${INSTALL_DIR}/rsim_server.log"
WEB_LOG="${INSTALL_DIR}/rsim_web.log"

c_red()    { printf "\033[31m%s\033[0m\n" "$1"; }
c_green()  { printf "\033[32m%s\033[0m\n" "$1"; }
c_yellow() { printf "\033[33m%s\033[0m\n" "$1"; }
c_cyan()   { printf "\033[36m%s\033[0m\n" "$1"; }

die() { c_red "ERROR: $1" >&2; exit 1; }

# --------------------------------------------------------------------------
# 前置检查
# --------------------------------------------------------------------------
preflight() {
    command -v python3 >/dev/null 2>&1 || die "python3 未安装（需要 Python 3.9+）"
    PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
    [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 9 ] || die "Python $PY_VER 版本过低（需要 3.9+）"
    c_green "Python $PY_VER OK"
    command -v git >/dev/null 2>&1 || die "git 未安装"
    c_green "git OK"
}

# --------------------------------------------------------------------------
# 克隆/更新代码
# --------------------------------------------------------------------------
fetch_code() {
    if [ -d "$INSTALL_DIR/.git" ]; then
        c_cyan "==> 更新已有代码 ($INSTALL_DIR)"
        git -C "$INSTALL_DIR" fetch --quiet origin main || die "git fetch 失败"
        git -C "$INSTALL_DIR" reset --hard origin/main >/dev/null 2>&1 || die "git reset 失败"
        c_green "代码已更新到最新"
    else
        c_cyan "==> 克隆代码到 $INSTALL_DIR"
        git clone --depth 1 "$REPO_URL" "$INSTALL_DIR" || die "git clone 失败"
        c_green "代码克隆完成"
    fi
}

# --------------------------------------------------------------------------
# 安装 PyYAML（web 前端需要；control server 不需要）
# --------------------------------------------------------------------------
ensure_pyyaml() {
    if python3 -c "import yaml" 2>/dev/null; then
        c_green "PyYAML 已安装"
        return
    fi
    c_cyan "==> 安装 PyYAML（web 前端需要）"
    if python3 -m pip install --user PyYAML >/dev/null 2>&1; then
        c_green "PyYAML 安装成功"
    else
        c_yellow "PyYAML 自动安装失败（web 前端将不可用；control server 仍可工作）"
        c_yellow "  手动装：python3 -m pip install PyYAML  或  sudo apt install python3-yaml"
    fi
}

# --------------------------------------------------------------------------
# 启动 control server（8877，cluster-only 白名单）
# --------------------------------------------------------------------------
start_server() {
    if [ -f "$SERVER_PID_FILE" ] && kill -0 "$(cat "$SERVER_PID_FILE")" 2>/dev/null; then
        c_yellow "control server 已在运行 (PID $(cat "$SERVER_PID_FILE"))"
        return
    fi
    c_cyan "==> 启动 control server (端口 $SERVER_PORT, cluster-only + 内置执行器)"
    mkdir -p "$DATA_DIR/results"
    (
        cd "$INSTALL_DIR"
        RSIM_HOME="$DATA_DIR" nohup python3 rsim.py server serve \
            --host 0.0.0.0 --port "$SERVER_PORT" \
            --allowed-task-types cluster.run \
            --cluster-executor \
            > "$SERVER_LOG" 2>&1 &
        echo $! > "$SERVER_PID_FILE"
    )
    sleep 2
    if curl -sf "http://127.0.0.1:$SERVER_PORT/health" >/dev/null 2>&1; then
        c_green "control server 启动成功 (PID $(cat "$SERVER_PID_FILE"))"
    else
        c_red "control server 启动失败，查看日志：$SERVER_LOG"
        tail -10 "$SERVER_LOG" 2>/dev/null
        exit 1
    fi
}

# --------------------------------------------------------------------------
# 启动 web 前端（8765，指向 8877，不起 agent）
# --------------------------------------------------------------------------
start_web() {
    if [ -f "$WEB_PID_FILE" ] && kill -0 "$(cat "$WEB_PID_FILE")" 2>/dev/null; then
        c_yellow "web 前端已在运行 (PID $(cat "$WEB_PID_FILE"))"
        return
    fi
    if ! python3 -c "import yaml" 2>/dev/null; then
        c_yellow "PyYAML 未安装，跳过 web 前端（control server 仍可用）"
        return
    fi
    c_cyan "==> 启动 web 前端 (端口 $WEB_PORT, 指向 127.0.0.1:$SERVER_PORT)"
    (
        cd "$INSTALL_DIR"
        nohup python3 rsim.py --project "$PROJECT" web \
            --host 0.0.0.0 --port "$WEB_PORT" \
            --server-url "http://127.0.0.1:$SERVER_PORT" \
            --no-control --user demo \
            > "$WEB_LOG" 2>&1 &
        echo $! > "$WEB_PID_FILE"
    )
    sleep 2
    if curl -sf -o /dev/null "http://127.0.0.1:$WEB_PORT/" 2>/dev/null; then
        c_green "web 前端启动成功 (PID $(cat "$WEB_PID_FILE"))"
    else
        c_yellow "web 前端启动可能失败，查看日志：$WEB_LOG"
        tail -10 "$WEB_LOG" 2>/dev/null
    fi
}

# --------------------------------------------------------------------------
# 停止服务
# --------------------------------------------------------------------------
stop_services() {
    for name in "control server" "web 前端"; do
        pidfile="$SERVER_PID_FILE"; [ "$name" = "web 前端" ] && pidfile="$WEB_PID_FILE"
        if [ -f "$pidfile" ]; then
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null && c_green "已停止 $name (PID $pid)"
            else
                c_yellow "$name 进程已不在 (PID $pid)"
            fi
            rm -f "$pidfile"
        else
            c_yellow "$name 未运行"
        fi
    done
}

# --------------------------------------------------------------------------
# 查看状态
# --------------------------------------------------------------------------
show_status() {
    c_cyan "=== radar-sim 服务状态 ==="
    # server
    if [ -f "$SERVER_PID_FILE" ] && kill -0 "$(cat "$SERVER_PID_FILE")" 2>/dev/null; then
        c_green "control server: 运行中 (PID $(cat "$SERVER_PID_FILE"), 端口 $SERVER_PORT)"
        curl -sf "http://127.0.0.1:$SERVER_PORT/health" && echo
    else
        c_red "control server: 未运行"
    fi
    # web
    if [ -f "$WEB_PID_FILE" ] && kill -0 "$(cat "$WEB_PID_FILE")" 2>/dev/null; then
        c_green "web 前端: 运行中 (PID $(cat "$WEB_PID_FILE"), 端口 $WEB_PORT)"
    else
        c_red "web 前端: 未运行"
    fi
    # 本机 IP
    c_cyan "=== 访问地址 ==="
    echo "本机 IP（供 Windows 访问）："
    hostname -I 2>/dev/null | awk '{print "  http://"$1":'$WEB_PORT'/  (前端)"; print "  http://"$1":'$SERVER_PORT'/ (后端 API)"}' || \
        ip addr show 2>/dev/null | grep -oP 'inet \K[0-9.]+' | grep -v '^127' | head -3 | \
        awk '{print "  http://"$0":'$WEB_PORT'/  (前端)"; print "  http://"$0":'$SERVER_PORT'/ (后端 API)"}'
}

# --------------------------------------------------------------------------
# 自检（白名单 + cluster.run）
# --------------------------------------------------------------------------
selftest() {
    c_cyan "=== 自检 ==="
    echo "1) health:"
    curl -sf "http://127.0.0.1:$SERVER_PORT/health" && echo
    echo "2) local task 应被拒绝 (400):"
    code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://127.0.0.1:$SERVER_PORT/api/jobs" \
        -H "Content-Type: application/json" -H "X-Rsim-User: smoke" \
        -d '{"job_type":"local.run_sim","payload":{"project":"'"$PROJECT"'"}}')
    [ "$code" = "400" ] && c_green "   OK (HTTP $code)" || c_red "   FAIL (HTTP $code, 期望 400)"
    echo "3) cluster.run 应被接受 (201):"
    code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://127.0.0.1:$SERVER_PORT/api/jobs" \
        -H "Content-Type: application/json" -H "X-Rsim-User: smoke" \
        -d '{"job_type":"cluster.run","payload":{"project":"'"$PROJECT"'","dataset":"BYD_SR","profile":"byd-ovrs-bl01v7-er-shared"}}')
    [ "$code" = "201" ] && c_green "   OK (HTTP $code)" || c_red "   FAIL (HTTP $code, 期望 201)"
    echo "4) web 前端:"
    code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$WEB_PORT/")
    [ "$code" = "200" ] && c_green "   OK (HTTP $code)" || c_yellow "   未就绪 (HTTP $code)"
}

# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
main() {
    local action="${1:-start}"
    case "$action" in
        stop)   stop_services; exit 0 ;;
        status) show_status; exit 0 ;;
        test)   selftest; exit 0 ;;
        start)  ;;
        --yes)  ;;
        *)      echo "用法: $0 [start|--yes|stop|status|test]"; exit 1 ;;
    esac

    c_cyan "========== radar-sim Linux 部署（模式 A：cluster-only）=========="
    preflight
    fetch_code
    ensure_pyyaml

    if [ "${1:-}" != "--yes" ]; then
        echo
        echo "将启动："
        echo "  - control server: 端口 $SERVER_PORT (cluster-only 白名单)"
        echo "  - web 前端:       端口 $WEB_PORT (指向 127.0.0.1:$SERVER_PORT)"
        echo "  - 安装目录:       $INSTALL_DIR"
        echo "  - 数据目录:       $DATA_DIR"
        read -r -p "继续？[y/N] " ans
        [ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "已取消"; exit 0; }
    fi

    start_server
    start_web
    echo
    show_status
    echo
    c_cyan "自检中..."
    selftest
    echo
    c_green "部署完成。"
    echo "日志：  $SERVER_LOG / $WEB_LOG"
    echo "停止：  bash scripts/linux_deploy.sh stop"
    echo "状态：  bash scripts/linux_deploy.sh status"
}

main "$@"
