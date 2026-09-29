#!/usr/bin/env bash
# AI 日程助手 · Cloudflare D1 一键部署
#
#   bash d1/setup.sh
#
# 它会依次做完：检查/安装 wr → 登录 → 自动填 account_id →
# 建库并自动填 database_id → 建表 → 生成并写入 WORKER_TOKEN → 部署 →
# 把 WORKER_URL / WORKER_TOKEN 写进 .streamlit/secrets.toml
#
# 全程只需要你在浏览器点一次授权，其余全自动。
# 想手动一步一步来，看同目录的 README-deploy.md。

set -euo pipefail
cd "$(dirname "$0")"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s▶ %s%s\n' "$YEL" "$*" "$OFF"; }
die()  { printf '%s✗ %s%s\n' "$RED" "$*" "$OFF" >&2; exit 1; }
ok()   { printf '%s✓ %s%s\n' "$GRN" "$*" "$OFF"; }

command -v node >/dev/null || die "没找到 node，请先装 Node.js 18+（https://nodejs.org）"

# ── 0. npm 镜像（国内直连官方源很慢）────────────────────────────────────────
# 不需要梯子。国内镜像同步官方源，内容完全一致，只是走 CDN 快很多。
REG="$(npm config get registry 2>/dev/null || echo '')"
case "$REG" in
  *npmmirror*|*tencent*|*huaweicloud*)
    ok "npm 镜像：$REG" ;;
  *)
    say "  当前 npm 源是 ${REG:-（默认）}，国内直连偏慢，切到国内镜像…"
    if npm config set registry https://registry.npmmirror.com >/dev/null 2>&1; then
      ok "已切到 https://registry.npmmirror.com（只影响下载速度，可随时用 npm config set registry 换回去）"
    else
      say "  ${DIM}切换失败，继续用当前源，可能慢一点${OFF}"
    fi ;;
esac
say "  ${DIM}提示：不要挂代理做 wrangler login——Cloudflare 对代理 IP 有风控，"
say "  挂代理反而更容易撞上 403 验证页。${OFF}"

# ── 1. wr ──────────────────────────────────────────────────────────────
# 不做 npm install -g。macOS 上全局装包要往 /usr/local/lib 写，普通用户没权限，
# 会直接 EACCES 失败。npx 把包装到用户目录里，不需要任何权限。
#
# 另外把 npm 缓存目录指到一处确定属于当前用户的位置：有些机器的 ~/.npm 里
# 混着 root 拥有的文件（以前跑过 sudo npm install -g 留下的），npm 一碰就
# EACCES。下面的环境变量把缓存挪开，绕开这个问题，且只对本脚本生效。
export npm_config_cache="${npm_config_cache:-$HOME/.cache/npm}"

wr() {
  if command -v wrangler >/dev/null 2>&1; then
    command wrangler "$@"
  else
    command npx --yes wrangler@latest "$@"
  fi
}

step "准备 wrangler"
if command -v wrangler >/dev/null 2>&1; then
  ok "已全局安装：$(wrangler --version 2>/dev/null | head -1)"
else
  say "  没有全局安装，改用 npx 运行（装在你自己目录，不需要管理员权限）"
  say "  第一次会下载 wrangler，大约十几秒…"
  if ! wr --version; then
    printf '%s\n' "${RED}下面是 wrangler 启动失败的真实报错：${OFF}" >&2
    say "  ${DIM}通常是这个原因之一：${OFF}"
    say "  ${DIM}  · node / npm 版本太老，npx 不认识 --yes 参数（需要 node 18+）${OFF}"
    say "  ${DIM}  · 网络到 npm 镜像不通${OFF}"
    say ""
    say "  ${DIM}先跑这条看看环境：${OFF}"
    say "    ${YEL}node -v && npm -v && npx --yes wrangler@latest --version${OFF}"
    exit 1
  fi
  ok "就绪（通过 npx）"
fi

# ── 2. 登录 ──────────────────────────────────────────────────────────────────
step "检查登录状态"
if ! wr whoami >/dev/null 2>&1; then
  say "  浏览器会弹出来，登录并点「Allow」…"
  wr login >/dev/null 2>&1 || die "wrangler login 失败。若报 403 或 bot challenge，先别重试——发我，我给你换 API Token 的方案"
fi
ok "已登录"

# ── 3. account_id ────────────────────────────────────────────────────────────
step "读取 Account ID"
WHOAMI="$(wr whoami 2>&1 || true)"
ACCOUNT_ID="$(printf '%s' "$WHOAMI" | grep -oE '\b[0-9a-f]{32}\b' | head -1 || true)"
[ -n "$ACCOUNT_ID" ] || die "没能从 wrangler whoami 里读到 Account ID。手动执行 wrangler whoami 看输出"
if grep -q '^account_id = "REPLACE' wrangler.toml; then
  # macOS 的 sed 需要 -i ''，Linux 不需要
  if [[ "$(uname)" == "Darwin" ]]; then
    sed -i '' "s|^account_id = \".*\"|account_id = \"$ACCOUNT_ID\"|" wrangler.toml
  else
    sed -i "s|^account_id = \".*\"|account_id = \"$ACCOUNT_ID\"|" wrangler.toml
  fi
  ok "已写入 wrangler.toml：$ACCOUNT_ID"
else
  ok "wrangler.toml 里已填好，未改动"
fi

# ── 4. database_id ───────────────────────────────────────────────────────────
step "准备数据库"
if grep -q '^database_id = "REPLACE' wrangler.toml; then
  say "  正在建库 schedule…"
  OUT="$(wr d1 create schedule 2>&1)" || { printf '%s\n' "$OUT" >&2; die "wrangler d1 create schedule 失败"; }
  DB_ID="$(printf '%s' "$OUT" | grep -oE '\b[0-9a-fA-F-]{36}\b' | head -1 || true)"
  [ -n "$DB_ID" ] || { printf '%s\n' "$OUT" >&2; die "输出里没找到 database_id，请手动填进 wrangler.toml"; }
  if [[ "$(uname)" == "Darwin" ]]; then
    sed -i '' "s|^database_id = \".*\"|database_id = \"$DB_ID\"|" wrangler.toml
  else
    sed -i "s|^database_id = \".*\"|database_id = \"$DB_ID\"|" wrangler.toml
  fi
  ok "已建库并写入 database_id：$DB_ID"
else
  ok "数据库已配置（$(grep '^database_id' wrangler.toml | cut -d'"' -f2)）"
  step "确认这个库确实存在"
  wr d1 execute schedule --remote --command "SELECT 1" >/dev/null 2>&1 \
    || die "wrangler.toml 里的 database_id 指向一个不存在的库。请核对，或删掉该行重跑本脚本"
  ok "库可访问"
fi

# ── 5. 建表 ──────────────────────────────────────────────────────────────────
step "建表"
wr d1 execute schedule --file=schema.sql --remote >/dev/null 2>&1 \
  || die "建表失败。手动执行：wrangler d1 execute schedule --file=schema.sql --remote"
ok "表已建好（重复执行是安全的）"

# ── 6. WORKER_TOKEN ──────────────────────────────────────────────────────────
step "配置密钥"
NEED_TOKEN=1
if wr secret list 2>/dev/null | grep -q WORKER_TOKEN; then
  ok "WORKER_TOKEN 已存在，保留原值"
  NEED_TOKEN=0
fi
if [ "$NEED_TOKEN" = "1" ]; then
  if command -v openssl >/dev/null; then
    TOKEN="$(openssl rand -hex 32)"
  else
    TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  fi
  printf '%s' "$TOKEN" | wr secret put WORKER_TOKEN >/dev/null 2>&1 \
    || die "wrangler secret put WORKER_TOKEN 失败"
  ok "已生成并写入 WORKER_TOKEN"
else
  # 保留原值时也得知道它，好写进 Streamlit
  TOKEN="$(cat .worker_token 2>/dev/null || true)"
  if [ -z "$TOKEN" ]; then
    say "  ${DIM}注意：WORKER_TOKEN 已存在但本地没有副本。若你不知道它的值，"
    say "  在 Cloudflare 控制台 → Workers & Pages → 你的 Worker → Settings →"
    say "  Variables and Secrets 里查看。${OFF}"
  fi
fi

# ── 7. 部署 ──────────────────────────────────────────────────────────────────
step "部署"
wr deploy 2>&1 | sed 's/^/  /'
URL="$(grep -oE 'https://[a-z0-9.-]+\.workers\.dev' .wrangler/deploy/*.json 2>/dev/null | head -1 || true)"
[ -n "$URL" ] || URL="$(wr deploy 2>&1 | grep -oE 'https://[a-z0-9.-]+\.workers\.dev' | head -1 || true)"
[ -n "$URL" ] || URL="（上面输出里的那个 https://xxx.workers.dev）"

# ── 8. 写进 Streamlit 的 secrets ─────────────────────────────────────────────
step "写入 .streamlit/secrets.toml"
SECRETS="../.streamlit/secrets.toml"
[ -f "$SECRETS" ] || die "找不到 $SECRETS"
if grep -q '^WORKER_URL' "$SECRETS"; then
  ok "secrets.toml 里已有 WORKER_URL，未改动"
else
  if [ -n "$TOKEN" ]; then
    cat >> "$SECRETS" <<EOF

# ── Cloudflare D1（由 d1/setup.sh 写入于 $(date '+%Y-%m-%d %H:%M')）──
WORKER_URL = "$URL"
WORKER_TOKEN = "$TOKEN"
EOF
    ok "已追加 WORKER_URL / WORKER_TOKEN"
  else
    ok "没拿到 token，没法自动写入，请手动把上面那两行加进 secrets.toml"
  fi
fi

printf '\n%s%s 部署完成 %s\n' "$DIM" "────────────────────────" "$OFF"
say ""
say "  Worker 地址 : $URL"
say ""
say "  ${YEL}还差最后一步：切换 app.py 的 import${OFF}"
say ""
say "  ${DIM}第 11 行：${OFF}"
say "    ${RED}- from sheets_storage import${OFF}  ${DIM}← 改成→${OFF}  ${GRN}+ from d1_storage import${OFF}"
say ""
say "  ${DIM}第 14 行：${OFF}"
say "    ${RED}- import sheets_storage${OFF}             ${DIM}← 改成→${OFF}  ${GRN}+ import d1_storage as sheets_storage${OFF}"
say ""
say "  然后回项目根目录迁移旧数据："
say "    ${YEL}cd .. && python3 d1/migrate_to_d1.py${OFF}          ${DIM}# 先预览${OFF}"
say "    ${YEL}python3 d1/migrate_to_d1.py --commit${OFF}         ${DIM}# 确认无误再写${OFF}"
say ""
say "  ${DIM}先别删 sheets_storage.py —— 万一要回退，它是你唯一的退路。${OFF}"
say ""
