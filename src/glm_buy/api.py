"""
直接 HTTP API 调用模块 — 使用 httpx 库绕过浏览器直接请求后端接口.

对应 JS 版中的 fetchProductIdDirectly() 和 probeSoldOutStatus() 函数.
用途：
  1. 在抢购开始前预获取 productId（不经过 Vue 数据流）
  2. 抢购窗口结束后探测服务端真实售罄状态
  3. 校验 bizId 是否过期
  4. 服务器时间校准

基础 URL: https://open.bigmodel.cn
测试时可通过 GLM_BUY_API_BASE 环境变量覆盖.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone  # 时间处理（服务器时间校准用）

import httpx  # 现代 Python HTTP 客户端（支持 HTTP/2，比 requests 更快）
from loguru import logger

# 智谱 AI 开放平台的 API 基础地址（测试时可通过环境变量覆盖）
BASE_URL = os.environ.get("GLM_BUY_API_BASE", "https://open.bigmodel.cn")


class APIClient:
  """
  智谱 BigModel 后端 API 的直连客户端.
  不经过浏览器/Playwright，直接用 httpx 发 HTTP 请求.
  """

  def __init__(self, auth_header: str | None = None) -> None:
    """
    初始化客户端.
    参数:
      auth_header: Authorization 请求头的值（如 "Bearer xxx"）.
                   从 Playwright 页面请求中自动捕获.
    """
    self._auth_header = auth_header
    # 创建 HTTP 客户端（10秒超时，自动跟随重定向）
    self._client = httpx.Client(
        timeout=httpx.Timeout(10.0),
        follow_redirects=True,
    )

  @property
  def auth_header(self) -> str | None:
    """获取 Authorization 头的值."""
    return self._auth_header

  @auth_header.setter
  def auth_header(self, value: str | None) -> None:
    """设置 Authorization 头的值（从浏览器请求中捕获）."""
    self._auth_header = value

  def _headers(self, extra: dict | None = None) -> dict:
    """
    构建标准请求头.
    包含 Content-Type、Accept，以及可选的 Authorization.
    """
    h = {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
    }
    if self._auth_header:
      h["Authorization"] = self._auth_header
    if extra:
      h.update(extra)
    return h

  def batch_preview(self) -> dict | None:
    """
    POST /api/biz/pay/batch-preview
    获取商品列表（包含所有套餐的价格、productId、售罄状态）.
    返回: API 响应的 JSON 字典，失败返回 None.
    """
    try:
      resp = self._client.post(
          f"{BASE_URL}/api/biz/pay/batch-preview",
          headers=self._headers(),
          json={},  # 空 body
      )
      if resp.is_success:  # HTTP 2xx
        return resp.json()
      logger.info(f"batch-preview 返回 HTTP {resp.status_code}")
      return None
    except Exception as e:
      logger.info(f"batch-preview 请求失败: {e}")
      return None

  def product_info(self) -> dict | None:
    """
    GET /api/biz/pay/productinfo
    备用商品信息接口（batch-preview 失败时的兜底）.
    返回的 JSON 结构是平铺的 key→productId.
    """
    try:
      resp = self._client.get(
          f"{BASE_URL}/api/biz/pay/productinfo",
          headers=self._headers(),
      )
      if resp.is_success:
        return resp.json()
      logger.info(f"productinfo 返回 HTTP {resp.json()}")
      return None
    except Exception as e:
      logger.info(f"productinfo 请求失败: {e}")
      return None

  def check_biz_id(self, biz_id: str) -> bool:
    """
    GET /api/biz/pay/check?bizId=...
    验证 bizId（业务 ID）是否过期.
    返回: True=有效, False=已过期(EXPIRE).
    """
    try:
      resp = self._client.get(
          f"{BASE_URL}/api/biz/pay/check",
          params={"bizId": biz_id},  # URL 查询参数
          headers=self._headers(),
      )
      data = resp.json()
      if data.get("data") == "EXPIRE":
        logger.info(f"[check] bizId={biz_id} 已过期")
        return False
      logger.debug(f"[check] bizId={biz_id} 校验通过")
      return True
    except Exception as e:
      # 校验异常时放行（宁可放过，不错杀）
      logger.debug(f"[check] 校验异常: {e}")
      return True

  def calibrate_time(self, samples: int = 3) -> int:
    """
    NTP 式多点采样法测量本地与服务器的时间偏差.

    每次 HEAD 请求记录 t1(发前) 和 t4(收后)，结合服务器 Date 头 t2:
      offset = t2 - (t1 + t4) / 2   (NTP 公式，对称延迟抵消)
      rtt    = t4 - t1

    取多次采样中偏移量最大的一次（网络延迟使测量值偏小，最大值最接近真实偏差）.
    返回: 偏差毫秒数（正数 = 本地比服务器慢）.
    """
    from email.utils import parsedate_to_datetime

    offsets: list[int] = []
    rtts: list[int] = []
    for i in range(samples):
      try:
        t1 = datetime.now(timezone.utc)
        resp = self._client.head(BASE_URL)
        t4 = datetime.now(timezone.utc)
        date_str = resp.headers.get("date", "")
        if not date_str:
          continue
        server_time = parsedate_to_datetime(date_str)
        if server_time.tzinfo is None:
          server_time = server_time.replace(tzinfo=timezone.utc)
        # NTP 偏移公式
        mid = t1 + (t4 - t1) / 2
        o = int((server_time - mid).total_seconds() * 1000)
        r = int((t4 - t1).total_seconds() * 1000)
        offsets.append(o)
        rtts.append(r)
        logger.info(
            f"[{i + 1}/{samples}] offset={o:+d}ms  rtt={r}ms"
        )
      except Exception as e:
        logger.info(f"[{i + 1}/{samples}] 采样失败: {e}")

    if not offsets:
      logger.info("时间校准失败：无有效采样")
      return 0

    # 取最大偏移（最接近真实时钟偏差）
    best = max(offsets)
    avg_rtt = sum(rtts) // len(rtts)
    logger.info(
        f"时间校准完成: offset={best:+d}ms  rtt≈{avg_rtt}ms  "
        f"({'本地慢' if best > 0 else '本地快'})  (共{len(offsets)}次采样)"
    )
    return best

  def close(self) -> None:
    """关闭 HTTP 客户端，释放连接."""
    self._client.close()
