#!/usr/bin/env bash
#
# dcselfie.sh - 웹 갤러리 운영 도구
#
# crawler / web / tunnel 을 macOS launchd 백그라운드 서비스로 돌린다.
# (화면에 안 보이게, 로그인 시 자동 시작, 죽으면 자동 재시작)
# 대시보드는 필요할 때만 띄운다.
#
#   ./dcselfie.sh install     서비스 plist 생성 + 등록 + 시작
#   ./dcselfie.sh start        서비스 시작
#   ./dcselfie.sh stop         서비스 정지
#   ./dcselfie.sh restart      서비스 재시작 (코드 바꾼 뒤)
#   ./dcselfie.sh status       상태 보기
#   ./dcselfie.sh logs         로그 실시간 보기 (Ctrl+C로 빠져나옴)
#   ./dcselfie.sh dash         대시보드 (Ctrl+C로 빠져나옴, 서비스는 계속 돔)
#   ./dcselfie.sh uninstall    서비스 등록 해제 + plist 삭제
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${DC_PYTHON:-$ROOT/venv/bin/python}"
[[ -x "$PY" ]] || { echo "Create venv and install requirements first." >&2; exit 1; }
umask 077
CFD="$(command -v cloudflared || true)"
LA="$HOME/Library/LaunchAgents"
LOGS="$ROOT/logs"
DOM="gui/$(id -u)"

# 필요하면 환경변수로 덮어쓰기 가능
TUNNEL="${DC_TUNNEL:-dcgallery}"
cd "$ROOT"
WEB_PORT="$("$PY" -c 'from Module.config import app_config; print(app_config.web_port)')"
STATIC_DIR="$ROOT/web_static"
MAINT="$ROOT/.maintenance"   # 존재하면 웹 서버가 점검 페이지를 보여줌

C_LABEL="win.dcselfie.crawler"
W_LABEL="win.dcselfie.web"
T_LABEL="win.dcselfie.tunnel"

emit_plist() {
  local label="$1" log="$2"; shift 2
  "$PY" "$ROOT/scripts/write_launchd_plist.py" "$LA/$label.plist" "$label" "$ROOT" "$log" "$@"
}

write_plists() {
  mkdir -p "$LA" "$LOGS"

  emit_plist "$C_LABEL" "$LOGS/crawler.log" \
    "$PY" "$ROOT/launcher.py"

  emit_plist "$W_LABEL" "$LOGS/web.log" \
    "$PY" "$ROOT/run_web_server.py"

  if [ -n "$CFD" ]; then
    emit_plist "$T_LABEL" "$LOGS/tunnel.log" \
      "$CFD" "tunnel" "--config" "$HOME/.cloudflared/config.yml" "run" "$TUNNEL"
  fi
}

labels() {
  echo "$C_LABEL" "$W_LABEL"
  if [ -f "$LA/$T_LABEL.plist" ]; then echo "$T_LABEL"; fi
}

svc_load() {
  for l in $(labels); do
    if ! launchctl print "$DOM/$l" >/dev/null 2>&1; then
      launchctl bootstrap "$DOM" "$LA/$l.plist"
    fi
  done
}
svc_unload() {
  for l in $(labels); do
    if launchctl print "$DOM/$l" >/dev/null 2>&1; then launchctl bootout "$DOM/$l"; fi
  done
}

cmd_install() {
  [ -f "$ROOT/.env" ] || { echo "Configure $ROOT/.env first." >&2; exit 1; }
  "$PY" "$ROOT/scripts/ensure_web_ingest_token.py" "$ROOT/.env"
  chmod 600 "$ROOT/.env"
  write_plists
  svc_unload
  svc_load
  echo "✅ 설치 완료. 서비스 등록됨:"
  cmd_status
}

cmd_start()   { svc_load;   echo "▶️  시작"; cmd_status; }
cmd_stop()    { svc_unload; echo "⏹  정지"; }
cmd_restart() { svc_unload; svc_load; cmd_status; }
cmd_uninstall(){ svc_unload; for l in $(labels); do rm -f "$LA/$l.plist"; done; echo "🗑  제거 완료"; }

cmd_status() {
  printf '%-22s %-10s %s\n' "SERVICE" "PID" "STATUS"
  for l in $(labels); do
    line="$(launchctl list 2>/dev/null | awk -v L="$l" '$3==L {print $1}')"
    if [ -n "$line" ] && [ "$line" != "-" ]; then
      printf '%-22s %-10s %s\n' "$l" "$line" "● running"
    elif launchctl list 2>/dev/null | grep -q "$l"; then
      printf '%-22s %-10s %s\n' "$l" "-" "○ loaded (대기/재시작중)"
    else
      printf '%-22s %-10s %s\n' "$l" "-" "✗ 미등록"
    fi
  done
  if curl --max-time 3 -fsS -o /dev/null -w '' "http://127.0.0.1:$WEB_PORT/healthz" 2>/dev/null; then
    echo "web: http://127.0.0.1:$WEB_PORT  →  https://dcselfie.win"
  fi
  if [ -f "$MAINT" ]; then
    echo "🛠  점검 모드: ON (사이트에 점검 페이지 노출 중 — 'up'으로 해제)"
  else
    echo "🟢 점검 모드: OFF (정상 운영)"
  fi
}

# 긴급 점검 on/off (웹 서버 재시작 불필요, 즉시 반영)
cmd_down() { touch "$MAINT"; echo "🛠  점검 모드 ON — https://dcselfie.win 에 점검 페이지가 표시됩니다."; }
cmd_up()   { rm -f "$MAINT"; echo "🟢 점검 모드 OFF — 사이트 정상 운영."; }

cmd_logs() { tail -n 40 -F "$LOGS"/crawler.log "$LOGS"/web.log "$LOGS"/tunnel.log 2>/dev/null; }
cmd_dash() { shift || true; exec "$PY" "$ROOT/dashboard.py" "$@"; }

case "${1:-}" in
  install)   cmd_install ;;
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  restart)   cmd_restart ;;
  status)    cmd_status ;;
  down)      cmd_down ;;
  up)        cmd_up ;;
  logs)      cmd_logs ;;
  dash)      cmd_dash "$@" ;;
  uninstall) cmd_uninstall ;;
  *) echo "사용법: $0 {install|start|stop|restart|status|down|up|logs|dash|uninstall}"; exit 1 ;;
esac
