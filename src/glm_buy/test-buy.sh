#!/usr/bin/env bash
# ============================================================
# GLM 抢购模拟测试脚本
#
# 用法:
#   chmod +x test-buy.sh
#   ./test-buy.sh                  # 默认：5 秒后开抢，有头模式
#   ./test-buy.sh 15               # 15 秒后开抢
#   ./test-buy.sh 10 headless      # 10 秒后开抢，无头模式
#   ./test-buy.sh 0                # 立即开抢（已过目标时间）
#
# 流程:
#   1. 启动 test_server.py（静态页面 + Mock API）
#   2. 等待服务就绪
#   3. 计算目标时间 = 当前时间 + DELAY 秒
#   4. 设置环境变量 → 启动抢购主程序
#   5. Ctrl+C 或程序退出后自动清理服务进程
# ============================================================

set -e

# ---- 参数 ----
DELAY="${1:-5}"                          # 多少秒后开抢，默认 5
HEADLESS="${2}"                          # "headless" 启用无头模式
PORT="${PORT:-8765}"                     # HTTP 服务端口，默认 8765

# ---- 路径 ----
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"   # test-buy.sh 所在目录 = src/glm_buy/
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)" # 项目根目录

TEST_PAGE="$SCRIPT_DIR/test_page.html"

# ---- 检查 test_page.html 是否存在 ----
if [ ! -f "$TEST_PAGE" ]; then
  echo "[错误] 找不到测试页面: $TEST_PAGE"
  exit 1
fi

# ---- 计算目标时间 (当前时间 + DELAY 秒) ----
# macOS 和 Linux 的 date 命令语法不同
if [[ "$OSTYPE" == "darwin"* ]]; then
  TARGET_TS=$(date -v+"${DELAY}S" +%s 2>/dev/null || echo 0)
else
  TARGET_TS=$(date -d "+${DELAY} seconds" +%s 2>/dev/null || echo 0)
fi

if [ "$TARGET_TS" = "0" ] || [ -z "$TARGET_TS" ]; then
  echo "[错误] 无法计算目标时间"
  exit 1
fi

TARGET_HOUR=$(date  -r "$TARGET_TS" +%H 2>/dev/null || date -d "@$TARGET_TS" +%H 2>/dev/null)
TARGET_MIN=$(date   -r "$TARGET_TS" +%M 2>/dev/null || date -d "@$TARGET_TS" +%M 2>/dev/null)
TARGET_SEC=$(date   -r "$TARGET_TS" +%S 2>/dev/null || date -d "@$TARGET_TS" +%S 2>/dev/null)

# ---- 清理函数 ----
cleanup() {
  echo ""
  echo "[清理] 正在关闭测试服务器 (PID=$SERVER_PID)..."
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  echo "[清理] 测试环境已清理"
}

trap cleanup EXIT INT TERM

# ---- 1. 启动测试服务器（静态页面 + Mock API）----
echo "============================================"
echo " GLM 抢购模拟测试"
echo "============================================"
echo "[服务] 启动测试服务器 (端口 $PORT)..."
cd "$SCRIPT_DIR"
python3 test_server.py "$PORT" &
SERVER_PID=$!

# 等待服务就绪（最多等 3 秒）
for i in $(seq 1 15); do
  if curl -s -o /dev/null "http://127.0.0.1:$PORT/test_page.html" 2>/dev/null; then
    echo "[服务] 测试服务器已就绪 (PID=$SERVER_PID)"
    break
  fi
  if [ "$i" -eq 15 ]; then
    echo "[错误] 测试服务器启动超时"
    exit 1
  fi
  sleep 0.2
done

# ---- 2. 设置环境变量 ----
export GLM_BUY_URL="http://127.0.0.1:$PORT/test_page.html"
export GLM_BUY_API_BASE="http://127.0.0.1:$PORT"
export GLM_BUY_TARGET_HOUR="$TARGET_HOUR"
export GLM_BUY_TARGET_MINUTE="$TARGET_MIN"
export GLM_BUY_TARGET_SECOND="$TARGET_SEC"

if [ "$HEADLESS" = "headless" ]; then
  export GLM_BUY_HEADLESS="1"
fi

echo ""
echo "[配置] 目标 URL:    $GLM_BUY_URL"
echo "[配置] API 基地址:  $GLM_BUY_API_BASE"
echo "[配置] 目标时间:    $TARGET_HOUR:$TARGET_MIN:$TARGET_SEC (${DELAY}s 后)"
echo "[配置] 无头模式:    ${GLM_BUY_HEADLESS:-否}"
echo "[配置] 测试页面已就绪，点击右下角 ⚙ 可切换售罄/验证码等状态"
echo ""

# ---- 3. 启动抢购程序 ----
cd "$PROJECT_DIR"
echo "[启动] 正在启动抢购主程序..."
echo "============================================"
echo ""

uv run python -m glm_buy.main

echo ""
echo "============================================"
echo " 测试结束"
echo "============================================"
