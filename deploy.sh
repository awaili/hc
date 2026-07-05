#!/usr/bin/env bash
# 本草链 HerbChain 部署脚本
# 用法: 在仓库根目录执行 ./deploy.sh [--no-pull]
#   默认先 git pull --ff-only;加 --no-pull 跳过(已手动拉过时用)
# 行为:
#   1. 备份现有 backend/nginx/systemd 文件到 backup-<时间戳>/
#   2. 复制代码到运行目录(绝不覆盖 *.env/view_token/chat_access 等密钥)
#   3. nginx -t 通过才 reload;失败自动回滚 nginx 配置
#   4. 重启 hc-survey;探活失败自动回滚 backend 并重启
#   5. 冒烟测试 https://hc.quickpapa.com/
set -euo pipefail

# --- 配置 ---
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_SRC="$REPO_DIR/backend"
FRONTEND_SRC="$REPO_DIR/frontend"
NGINX_SRC="$REPO_DIR/nginx"
SYSTEMD_SRC="$REPO_DIR/systemd"

BACKEND_DST="/opt/hc-survey"
FRONTEND_DST="/data/website/hc.quickpapa.com"
NGINX_DST="/etc/nginx/conf.d/hc.quickpapa.com.conf"
SYSTEMD_DST="/etc/systemd/system/hc-survey.service"

TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$REPO_DIR/backup-$TS"
HOST="hc.quickpapa.com"

c_red() { printf "\033[31m%s\033[0m\n" "$1"; }
c_grn() { printf "\033[32m%s\033[0m\n" "$1"; }
c_ylw() { printf "\033[33m%s\033[0m\n" "$1"; }
log() { printf "▶ %s\n" "$1"; }

# --- 前置检查 ---
[ -f "$BACKEND_SRC/app.py" ] || { c_red "不在仓库根目录(找不到 backend/app.py)"; exit 1; }
[ "$(id -u)" = "0" ] || { c_red "需要 root 权限(读写 /opt /etc)"; exit 1; }

# --- git pull ---
if [ "${1:-}" != "--no-pull" ]; then
  log "拉取最新代码..."
  cd "$REPO_DIR"
  git pull --ff-only || { c_ylw "git pull 失败(可能有本地改动或需先 commit);用 --no-pull 跳过"; exit 1; }
fi

# --- 备份 ---
log "备份现有文件到 $BACKUP_DIR"
mkdir -p "$BACKUP_DIR/backend" "$BACKUP_DIR/nginx" "$BACKEND_DST" 2>/dev/null || true
cp -p "$BACKEND_DST/app.py" "$BACKEND_DST/system_prompt.md" "$BACKUP_DIR/backend/" 2>/dev/null || true
cp -p "$NGINX_DST" "$BACKUP_DIR/nginx/" 2>/dev/null || true
cp -p "$SYSTEMD_DST" "$BACKUP_DIR/" 2>/dev/null || true
cp -rp "$FRONTEND_DST" "$BACKUP_DIR/frontend" 2>/dev/null || true

rollback_backend() {
  c_red "hc-survey 探活失败,回滚 backend..."
  cp -p "$BACKUP_DIR/backend/app.py" "$BACKEND_DST/" 2>/dev/null || true
  cp -p "$BACKUP_DIR/backend/system_prompt.md" "$BACKEND_DST/" 2>/dev/null || true
  systemctl restart hc-survey.service || true
  c_ylw "已回滚 backend。nginx/前端已是新版本,可手动处理。"
}

# --- 部署 backend(只覆盖 app.py 和 system_prompt.md,密钥不动) ---
log "部署 backend -> $BACKEND_DST"
install -m 644 "$BACKEND_SRC/app.py" "$BACKEND_DST/app.py"
install -m 644 "$BACKEND_SRC/system_prompt.md" "$BACKEND_DST/system_prompt.md"
# 校验语法
python3 -m py_compile "$BACKEND_DST/app.py" || { c_red "app.py 语法错误,中止"; rollback_backend; exit 1; }

# --- 部署 systemd unit ---
log "部署 systemd unit"
install -m 644 "$SYSTEMD_SRC/hc-survey.service" "$SYSTEMD_DST"
systemctl daemon-reload

# --- 部署前端 ---
log "部署 frontend -> $FRONTEND_DST"
mkdir -p "$FRONTEND_DST"
install -m 644 "$FRONTEND_SRC"/*.html "$FRONTEND_DST/"
install -m 644 "$FRONTEND_SRC"/*.js "$FRONTEND_DST/" 2>/dev/null || true
install -m 644 "$FRONTEND_SRC"/*.css "$FRONTEND_DST/" 2>/dev/null || true

# --- 部署 nginx(先 -t,失败回滚) ---
log "部署 nginx 配置(先校验)"
install -m 644 "$NGINX_SRC/hc.quickpapa.com.conf" "$NGINX_DST"
if ! nginx -t 2>&1 | tail -3; then
  c_red "nginx -t 失败,回滚配置"
  cp -p "$BACKUP_DIR/nginx/hc.quickpapa.com.conf" "$NGINX_DST"
  nginx -t && systemctl reload nginx || c_ylw "回滚后仍失败,请手动检查"
  exit 1
fi
systemctl reload nginx
c_grn "nginx 已 reload"

# --- 重启后端 + 探活 ---
log "重启 hc-survey 并探活"
systemctl restart hc-survey.service
sleep 2
if ! systemctl is-active --quiet hc-survey.service; then
  rollback_backend
  c_red "hc-survey 未起来,已回滚。日志: journalctl -u hc-survey -n 30"
  exit 1
fi
if ! curl -sf --max-time 5 http://127.0.0.1:5006/ >/dev/null; then
  rollback_backend
  c_red "hc-survey 探活(curl)失败,已回滚"
  exit 1
fi
c_grn "hc-survey 已运行并探活通过"

# --- 冒烟测试公网 ---
log "冒烟测试 https://$HOST/"
for path in "/" "/chat.html" "/results.html"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "https://$HOST$path" || echo "ERR")
  [ "$code" = "200" ] && c_grn "  $path -> $code" || c_ylw "  $path -> $code (非200)"
done

echo
c_grn "✅ 部署完成 @ $TS"
echo "备份在: $BACKUP_DIR  (确认无误后可删: rm -rf $BACKUP_DIR)"
echo "下次更新: cd $REPO_DIR && ./deploy.sh"