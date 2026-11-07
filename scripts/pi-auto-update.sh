#!/usr/bin/env bash
# Tự cập nhật code trên Pi (lockr-tu01) theo nhánh main của repo iot — chạy mỗi 5 phút bởi lockr-auto-update.timer
# (cài bằng scripts/install-pi-auto-update.sh). Log: journalctl -u lockr-auto-update
#
#  - Chỉ kéo fast-forward. Có thay đổi chưa commit, hoặc đang ở nhánh khác main ⇒ bỏ qua (không làm mất code sửa tay).
#  - ui/ đổi ⇒ build kiosk sang ui/dist.new; build lỗi thì giữ nguyên bản đang chạy. Build xong mới đổi thư mục,
#    khởi động lại lockr-kiosk và đóng Chromium để ~/kiosk.sh tự mở lại trang mới.
#  - Code Python / pyproject / uv.lock đổi ⇒ uv sync (nếu cần) và khởi động lại lockr-controller.
#    Thay đổi chỉ ở ui/ hay docs thì KHÔNG khởi động lại bộ điều khiển tủ.
#  - Cấu hình riêng của máy (ui/.env.local, .env) nằm ngoài git nên không bị ghi đè.
set -euo pipefail

USER_NAME="${LOCKR_USER:-lockr}"
REPO="${LOCKR_REPO:-/home/$USER_NAME/iot}"
BRANCH=main
LOCK=/run/lockr-auto-update.lock

as_user() { runuser -u "$USER_NAME" -- env HOME="/home/$USER_NAME" PATH="/home/$USER_NAME/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }
log() { echo "[auto-update] $*"; }

exec 9>"$LOCK"
flock -n 9 || { log "lần chạy trước chưa xong — bỏ qua"; exit 0; }

cd "$REPO"
cur_branch="$(as_user git rev-parse --abbrev-ref HEAD)"
if [ "$cur_branch" != "$BRANCH" ]; then log "đang ở nhánh $cur_branch, không phải $BRANCH — bỏ qua"; exit 0; fi
if [ -n "$(as_user git status --porcelain --untracked-files=no)" ]; then
  log "~/iot có thay đổi chưa commit — bỏ qua để không mất code (xem: git status)"; exit 0
fi

as_user git fetch -q origin "$BRANCH"
old="$(as_user git rev-parse HEAD)"
new="$(as_user git rev-parse "origin/$BRANCH")"
[ "$old" = "$new" ] && exit 0
if ! as_user git merge-base --is-ancestor "$old" "$new"; then
  log "HEAD $old không nằm trên origin/$BRANCH (lịch sử bị viết lại?) — bỏ qua, cần xử lý tay"; exit 0
fi

changed="$(as_user git diff --name-only "$old" "$new")"
log "cập nhật ${old:0:7} → ${new:0:7} ($(echo "$changed" | wc -l) file)"
as_user git merge -q --ff-only "$new"

# ── Kiosk ────────────────────────────────────────────────────────────────────────────────────────
if echo "$changed" | grep -q '^ui/'; then
  cd "$REPO/ui"
  if echo "$changed" | grep -qE '^ui/(package\.json|package-lock\.json)$'; then
    log "package-lock đổi — npm ci"
    as_user npm ci --no-audit --no-fund --silent
  fi
  rm -rf dist.new
  if as_user npx vite build --outDir dist.new --emptyOutDir --logLevel error; then
    rm -rf dist.old
    [ -d dist ] && mv dist dist.old
    mv dist.new dist
    sync
    systemctl restart lockr-kiosk
    # ~/kiosk.sh mở lại Chromium khi nó thoát ⇒ màn hình tải trang mới mà không phải khởi động lại phiên desktop
    sleep 3
    pkill -u "$USER_NAME" -f 'chromium.*--kiosk' || true
    rm -rf dist.old
    log "kiosk đã build và tải lại"
  else
    rm -rf dist.new
    log "LỖI build kiosk — giữ bản đang chạy"
  fi
  cd "$REPO"
fi

# ── Bộ điều khiển tủ (Python) ────────────────────────────────────────────────────────────────────
if echo "$changed" | grep -qvE '^(ui/|docs?/|.*\.md$|tests/|arduino/|drone-iot/|\.github/|scripts/(pi-auto-update|install-pi-auto-update)\.sh$|(debug|simulate|test)_[^/]*\.py$)'; then
  if echo "$changed" | grep -qE '^(pyproject\.toml|uv\.lock)$'; then
    log "phụ thuộc Python đổi — uv sync"
    as_user uv sync -q
  fi
  systemctl restart lockr-controller
  log "đã khởi động lại lockr-controller"
fi

sync
log "xong: $(as_user git log --oneline -1)"
