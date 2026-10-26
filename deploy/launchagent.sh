#!/bin/bash
# WaveType 登录自启（LaunchAgent）管理脚本
#
#   bash deploy/launchagent.sh install     安装并立即启动（登录后自动运行）
#   bash deploy/launchagent.sh uninstall   卸载（含当前运行的实例）
#   bash deploy/launchagent.sh restart     改完 config.yaml 后重启生效
#   bash deploy/launchagent.sh status      看运行状态
#   bash deploy/launchagent.sh logs        跟踪日志（Ctrl+C 退出）
#   bash deploy/launchagent.sh perms       打开三个权限设置面板
#   bash deploy/launchagent.sh reveal      在访达里选中解释器（拖进权限列表用）
#
# 注意：装好之后不要再手动跑 start.sh，否则两个实例会抢同一个 Fn 热键。

set -uo pipefail

LABEL="com.robin.wavetype"
PROJECT_DIR="/Users/robinwang/Coding/语音输入本地部署"
PY_VENV="$PROJECT_DIR/.venv/bin/python3"
PY_REAL="/Users/robinwang/.workbuddy/binaries/python/versions/3.13.12/bin/python3.13"
PLIST_SRC="$PROJECT_DIR/deploy/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/WaveType"
LOG_MAIN="$LOG_DIR/wavetype.log"
DOMAIN="gui/$(id -u)"

# 真实解释器存在才提示（软链被解析后 TCC 可能认的是它）
REAL_PY=""
[ -x "$PY_REAL" ] && REAL_PY="$PY_REAL"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

need_bash_not_sh() {
  if [ -z "${BASH_VERSION:-}" ]; then
    say "请用 bash 运行：bash deploy/launchagent.sh $1"
    exit 1
  fi
}

do_install() {
  say "== 安装 $LABEL =="

  [ -f "$PLIST_SRC" ] || { say "找不到 $PLIST_SRC"; exit 1; }
  [ -x "$PROJECT_DIR/.venv/bin/python3" ] || { say "找不到 venv 解释器，先跑一次 ./start.sh"; exit 1; }
  [ -f "$PROJECT_DIR/config.yaml" ] || { say "缺少 config.yaml"; exit 1; }

  mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

  cp "$PLIST_SRC" "$PLIST_DST"
  plutil -lint "$PLIST_DST" >/dev/null || { say "plist 格式有问题"; exit 1; }
  ok "plist 已放到 $PLIST_DST"

  # 已加载就先卸掉，否则 bootstrap 会报 Input/output error
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && warn "已卸下旧实例"

  err="$(launchctl bootstrap "$DOMAIN" "$PLIST_DST" 2>&1)"
  if [ -z "$err" ]; then
    ok "已注册到 launchd（登录自启）"
  else
    say "bootstrap 失败：$err"
    say ""
    case "$err" in
      *"Input/output error"*)
        warn "Input/output error 一般是因为这段命令跑在沙箱里（比如 WorkBuddy）——"
        warn "请在「终端.app」里重新执行本脚本。" ;;
      *"already bootstrapped"*|*"service already loaded"*)
        warn "服务已在加载状态，直接跑 restart 即可。" ;;
    esac
    exit 1
  fi

  launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1
  sleep 4

  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    ok "服务已加载（登录后自动在后台待命）"
  fi

  say ""
  say "== 还差一步：把权限授给解释器（只有第一次要做）=="
  say "macOS 权限绑在启动它的程序上，之前授给终端的不算数了。"
  say ""
  warn "注意：解释器在 .venv 里，而 .venv 是「点开头」的隐藏目录，"
  warn "权限面板的 + 号默认开在 /Applications，所以你翻不到它。"
  say ""
  say "最省事的做法——直接拖："
  say "  1) 跑  bash deploy/launchagent.sh reveal   （会在访达里选中解释器）"
  say "  2) 把访达里那个 python3 直接拖进权限列表"
  say ""
  say "或者在选择框里按 ⌘⇧G（前往文件夹），粘贴："
  say "  ${PROJECT_DIR}/.venv/bin"
  say "再选 python3。（⌘⇧. 可以切换显示隐藏文件）"
  say ""
  if [ -n "$REAL_PY" ]; then
    say "万一 python3 是软链、选择框里点不动，就把这个真实文件也加进去："
    say "  ${REAL_PY}"
  fi
  say ""
  warn "不授权的话：Fn 按了没反应。日志里会留一条 CGEventTapCreate 失败的记录。"
  say ""
  say "== 授权之后必须重启服务 =="
  say "热键线程已经因为没权限退出了，光授权不会自动恢复："
  say "  bash deploy/launchagent.sh restart"
  say ""
  say "== 验证 =="
  say "  看日志：bash deploy/launchagent.sh logs"
  say "  出现『热键监听已启动』才算接上；再按 Fn 应能看到 ASR/润色记录"
}

do_uninstall() {
  say "== 卸载 $LABEL =="
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && ok "已停止并注销" || warn "服务本来就没在跑"
  if [ -f "$PLIST_DST" ]; then
    rm -f "$PLIST_DST"
    ok "已删除 $PLIST_DST"
  fi
  say "日志保留在 $LOG_DIR"
  say "（Terminal 里手动授权的那份记录不受影响）"
}

do_restart() {
  say "== 重启 ${LABEL}（改完 config.yaml 用这个）=="
  if ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    say "服务未安装，先跑：bash deploy/launchagent.sh install"
    exit 1
  fi
  launchctl kickstart -k "$DOMAIN/$LABEL"
  ok "已重启"
}

do_status() {
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    ok "服务已加载"
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
      | grep -E '^\s*(state|pid|last exit code|program) ' | sed 's/^/    /'
  else
    warn "服务未加载（没装，或已 bootout）"
  fi
  say ""
  say "最近的日志："
  if [ -f "$LOG_MAIN" ]; then
    tail -n 12 "$LOG_MAIN" | sed 's/^/    /'
  else
    say "    （还没有日志：${LOG_MAIN}）"
  fi
}

do_logs() {
  say "跟踪 ${LOG_MAIN}（Ctrl+C 退出）"
  warn "注意：日志里有你说过的内容明文"
  say ""
  tail -f "$LOG_MAIN"
}

do_reveal() {
  say "在访达中定位解释器（拖进权限列表用）"
  say ""
  say "  虚拟环境软链：$PY_VENV"
  if open -R "$PY_VENV" 2>/dev/null; then
    ok "已打开访达并选中它 —— 直接拖进权限列表即可"
  else
    warn "访达没打开（沙箱/权限限制），用 ⌘⇧G 手动前往"
  fi
  say ""
  if [ -n "$REAL_PY" ]; then
    say "  真实解释器（软链点不动时加这个）："
    say "    $REAL_PY"
    open -R "$REAL_PY" 2>/dev/null || true
  fi
  say ""
  say "手动前往办法：访达/选择框里按 ⌘⇧G，粘贴路径后回车"
  say "  ${PROJECT_DIR}/.venv/bin"
  warn ".venv 是点开头的隐藏目录，直接翻是看不到的（⌘⇧. 可切换显示）"
}

do_perms() {
  say "正在打开三个权限面板…"
  for pane in Privacy_Microphone Privacy_ListenEvent Privacy_Accessibility; do
    open "x-apple.systempreferences:com.apple.preference.security?$pane" 2>/dev/null \
      || warn "没能自动打开 $pane，手动去 系统设置 → 隐私与安全性"
    sleep 1
  done
  say ""
  say "要加的文件："
  say "  $PY_VENV"
  [ -n "$REAL_PY" ] && say "  $REAL_PY   （备选）"
  say ""
  say "找不到就先用 reveal 在访达里选出来，再拖进列表："
  say "  bash deploy/launchagent.sh reveal"
}

need_bash_not_sh "${1:-install}"

case "${1:-install}" in
  install)   do_install ;;
  uninstall) do_uninstall ;;
  restart)   do_restart ;;
  status)    do_status ;;
  logs)      do_logs ;;
  perms)     do_perms ;;
  reveal)    do_reveal ;;
  *)         say "用法: bash deploy/launchagent.sh {install|uninstall|restart|status|logs|perms|reveal}" ;;
esac
