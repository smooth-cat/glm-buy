"""
GLM 抢购模拟测试服务器.

同时提供:
  1. 静态文件服务 (test_page.html)
  2. Mock API 端点 (batch-preview / productinfo / HEAD)

用法:
  python test_server.py                # 默认端口 8765
  python test_server.py 9000           # 自定义端口
  GLM_PORT=9000 python test_server.py  # 环境变量方式
"""

import json
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path


# ---- Mock 数据 ----

# batch-preview 响应: productList 包含 3 个套餐 × 3 个周期的完整数据
MOCK_PRODUCT_LIST = [
  {
    "productId": "product-lite-monthly",
    "monthlyOriginalAmount": 49,
    "soldOut": False,
    "isSoldOut": False,
    "campaignDiscountDetails": [{"campaignName": "连续包月"}],
  },
  {
    "productId": "product-lite-quarterly",
    "monthlyOriginalAmount": 49,
    "soldOut": False,
    "isSoldOut": False,
    "campaignDiscountDetails": [{"campaignName": "包季特惠"}],
  },
  {
    "productId": "product-lite-yearly",
    "monthlyOriginalAmount": 49,
    "soldOut": False,
    "isSoldOut": False,
    "campaignDiscountDetails": [{"campaignName": "包年大促"}],
  },
  {
    "productId": "product-pro-monthly",
    "monthlyOriginalAmount": 149,
    "soldOut": False,
    "isSoldOut": False,
    "campaignDiscountDetails": [{"campaignName": "连续包月"}],
  },
  {
    "productId": "product-pro-quarterly",
    "monthlyOriginalAmount": 149,
    "soldOut": False,
    "isSoldOut": False,
    "campaignDiscountDetails": [{"campaignName": "包季特惠"}],
  },
  {
    "productId": "product-pro-yearly",
    "monthlyOriginalAmount": 149,
    "soldOut": False,
    "isSoldOut": False,
    "campaignDiscountDetails": [{"campaignName": "包年大促"}],
  },
  {
    "productId": "product-max-monthly",
    "monthlyOriginalAmount": 469,
    "soldOut": False,
    "isSoldOut": False,
    "campaignDiscountDetails": [{"campaignName": "连续包月"}],
  },
  {
    "productId": "product-max-quarterly",
    "monthlyOriginalAmount": 469,
    "soldOut": False,
    "isSoldOut": False,
    "campaignDiscountDetails": [{"campaignName": "包季特惠"}],
  },
  {
    "productId": "product-max-yearly",
    "monthlyOriginalAmount": 469,
    "soldOut": False,
    "isSoldOut": False,
    "campaignDiscountDetails": [{"campaignName": "包年大促"}],
  },
]

# productinfo 响应: 平铺 key → productId
MOCK_PRODUCT_INFO = {
  "liteMonthly": "product-lite-monthly",
  "liteQuarterly": "product-lite-quarterly",
  "liteYearly": "product-lite-yearly",
  "proMonthly": "product-pro-monthly",
  "proQuarterly": "product-pro-quarterly",
  "proYearly": "product-pro-yearly",
  "maxMonthly": "product-max-monthly",
  "maxQuarterly": "product-max-quarterly",
  "maxYearly": "product-max-yearly",
}


class TestHandler(SimpleHTTPRequestHandler):
  """
  自定义请求处理器: API 路径返回 mock JSON, 其他走静态文件.
  """

  def __init__(self, *args, **kwargs):
    kwargs.setdefault("directory", str(SCRIPT_DIR))
    super().__init__(*args, **kwargs)

  # ---- API 路由 ----

  def do_POST(self):
    if self.path == "/api/biz/pay/batch-preview":
      self._json_response({"productList": MOCK_PRODUCT_LIST})
    elif self.path.startswith("/api/biz/pay/preview"):
      self._json_response({"ok": True, "message": "订单预览成功"})
    elif self.path.startswith("/api/biz/pay/submit"):
      self._json_response({"ok": True, "message": "订单提交成功", "bizId": "biz-mock-001"})
    else:
      self.send_error(404)

  def do_GET(self):  # type: ignore[override]
    if self.path == "/api/biz/pay/productinfo":
      self._json_response(MOCK_PRODUCT_INFO)
    elif self.path == "/api/biz/pay/check":
      self._json_response({"data": "OK"})
    else:
      super().do_GET()

  def do_HEAD(self):  # type: ignore[override]
    """HEAD / 用于时间校准."""
    if self.path == "/":
      self._send_head_date()
    else:
      super().do_HEAD()

  # ---- 辅助 ----

  def _json_response(self, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    self.send_response(200)
    self.send_header("Content-Type", "application/json;charset=UTF-8")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Access-Control-Allow-Origin", "*")
    self.end_headers()
    self.wfile.write(body)

  def _send_head_date(self):
    """返回 HTTP Date 头（供 api.py 的 calibrate_time 使用）."""
    from email.utils import formatdate
    self.send_response(200)
    self.send_header("Date", formatdate(usegmt=True))
    self.end_headers()

  def log_message(self, fmt, *args):
    """重写日志格式: [API] 前缀标记 mock 接口."""
    if "api/biz/pay" in (args[0] if args else ""):
      print(f"  [API] {args[0]}" if args else "")
    else:
      print(f"  [静态] {args[0]}" if args else "")


# ---- 入口 ----

SCRIPT_DIR = Path(__file__).resolve().parent

if __name__ == "__main__":
  port = int(os.environ.get("GLM_PORT", sys.argv[1] if len(sys.argv) > 1 else "8765"))
  server = HTTPServer(("127.0.0.1", port), TestHandler)
  print(f"============================================")
  print(f" GLM 模拟测试服务器")
  print(f"============================================")
  print(f" 地址: http://127.0.0.1:{port}/test_page.html")
  print(f" API:  http://127.0.0.1:{port}/api/biz/pay/batch-preview")
  print(f"       http://127.0.0.1:{port}/api/biz/pay/productinfo")
  print(f"")
  print(f" 按 Ctrl+C 停止")
  print(f"============================================")
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    print("\n服务器已停止")
    server.server_close()
