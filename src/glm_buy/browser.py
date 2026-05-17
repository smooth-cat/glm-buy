"""
Playwright 浏览器管理 — 启动/停止浏览器 + 请求拦截.

这是 JS → Python 移植中最关键的一层，实现了 JS 版的核心"欺骗"逻辑：
  JS 版通过劫持 JSON.parse / fetch / XHR 来修改 API 响应中的 soldOut 字段
  Python 版通过 Playwright 的 page.route() 网络层拦截实现同样效果

page.route() 的优势：
  - 在浏览器网络层拦截，不需要修改 JS 运行时
  - 可以同时修改请求（如注入 productId）和响应（如改 soldOut=false）
  - 比 JS 版的 Monkey Patching 更干净、更稳定

核心拦截逻辑仅在每天的 10:00-10:02 抢购窗口期生效.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from datetime import datetime
from pathlib import Path

from loguru import logger
from playwright.async_api import (
    Browser,         # Playwright 浏览器实例
    BrowserContext,  # 浏览器上下文（隔离的会话）
    Page,            # 标签页
    async_playwright, # Playwright 入口函数
)


class BrowserManager:
  """
  管理 Playwright 浏览器的完整生命周期.
  负责：启动浏览器 → 设置请求拦截 → 处理响应修改 → 关闭浏览器.
  """

  def __init__(
      self,
      headless: bool = False,
      user_data_dir: str = "browser_profile",
      purchase_url: str = "https://open.bigmodel.cn/glm-coding",
      target_hour: int = 10,
      target_minute: int = 0,
      target_second: int = 0,
  ) -> None:
    """
    初始化浏览器管理器.

    参数:
      headless:       True = 无头模式（不可见），False = 可见窗口（鼠标模拟需要可见）
      user_data_dir:  浏览器用户数据目录（保存 Cookie/登录态）
      purchase_url:   购买页面 URL
      target_hour/minute/second: 目标抢购时间（用于判断 rush window）
    """
    self._headless = headless
    self._user_data_dir = Path(user_data_dir).resolve()  # 转绝对路径
    self._purchase_url = purchase_url
    self._target_hour = target_hour
    self._target_minute = target_minute
    self._target_second = target_second

    # 运行时对象（初始为 None，start() 后赋值）
    self._playwright = None          # Playwright 实例
    self._browser: Browser | None = None
    self._context: BrowserContext | None = None  # 浏览器上下文
    self._page: Page | None = None               # 当前标签页

    # 从页面请求中捕获的数据
    self._auth_header: str | None = None          # Authorization 请求头
    self._product_id: str | None = None           # 要注入的 productId

    # 状态标志（由 scheduler 控制）
    self._confirm_sold_out: bool = False          # 是否确认售罄（停止拦截）
    self._order_created: bool = False             # 订单是否已创建（停止点击）
    self._force_pay_dialog_called: bool = False   # 是否已触发支付弹窗

  # ==================== 属性访问器 ====================

  @property
  def page(self) -> Page | None:
    """获取当前 Playwright 页面对象."""
    return self._page

  @property
  def auth_header(self) -> str | None:
    """获取捕获的 Authorization 请求头（供 api.py 使用）."""
    return self._auth_header

  @property
  def product_id(self) -> str | None:
    """获取当前用于注入的 productId."""
    return self._product_id

  @product_id.setter
  def product_id(self, value: str | None) -> None:
    """scheduler 设置 productId 后会同步到这里，供请求拦截使用."""
    self._product_id = value

  @property
  def confirm_sold_out(self) -> bool:
    return self._confirm_sold_out

  @confirm_sold_out.setter
  def confirm_sold_out(self, value: bool) -> None:
    """scheduler 确认售罄后调用，停止响应修改."""
    self._confirm_sold_out = value

  @property
  def order_created(self) -> bool:
    return self._order_created

  @order_created.setter
  def order_created(self, value: bool) -> None:
    """scheduler 检测到支付二维码后调用."""
    self._order_created = value

  @property
  def force_pay_dialog_called(self) -> bool:
    return self._force_pay_dialog_called

  @force_pay_dialog_called.setter
  def force_pay_dialog_called(self, value: bool) -> None:
    self._force_pay_dialog_called = value

  # ==================== 浏览器生命周期 ====================

  async def start(self) -> Page:
    """启动浏览器并导航到购买页面."""
    # 第一步：启动 Playwright
    logger.info("正在启动 Playwright...")
    self._playwright = await async_playwright().start()

    # 确保用户数据目录存在
    self._user_data_dir.mkdir(parents=True, exist_ok=True)

    # 第二步：启动 Chromium 持久化上下文
    logger.info("正在启动 Chromium 浏览器（首次启动可能较慢，请耐心等待）...")
    try:
      # launch_persistent_context:
      #   - 持久化上下文会保存 Cookie/登录态到 user_data_dir
      #   - 下次启动时自动恢复，无需重新登录
      self._context = await asyncio.wait_for(
          self._playwright.chromium.launch_persistent_context(
              user_data_dir=str(self._user_data_dir),  # 持久化目录
              headless=self._headless,                  # 是否无头
              viewport={"width": 1280, "height": 800},  # 窗口大小
              locale="zh-CN",                            # 中文界面
              args=[
                  "--no-first-run",                    # 跳过首次运行向导
                  "--no-default-browser-check",        # 跳过默认浏览器检查
                  "--disable-features=Translate",       # 禁用翻译提示
                  "--disable-background-networking",    # 减少后台网络
              ],
          ),
          timeout=60.0,  # 60秒超时（首次启动可能较慢）
      )
    except asyncio.TimeoutError:
      # 超时：给出排查指引
      logger.error("浏览器启动超时 (60s)，请检查：")
      logger.error("  1. 是否已安装 Chromium: uv run playwright install chromium")
      logger.error("  2. 是否有其他 Chromium 实例占用了浏览器配置文件")
      logger.error("  3. 尝试删除 browser_profile/ 目录后重试")
      raise
    except Exception as e:
      msg = str(e)
      if "Target closed" in msg or "Browser closed" in msg:
        logger.error(f"浏览器意外关闭: {e}")
      raise

    # 第三步：创建新标签页并设置请求拦截
    self._page = await self._context.new_page()
    await self._setup_routes()       # 设置 API 响应拦截
    await self._setup_auth_capture()  # 设置 Authorization 头捕获

    # 第四步：导航到购买页面
    logger.info("浏览器已启动，正在打开购买页面...")
    await self._page.goto(self._purchase_url, wait_until="domcontentloaded")
    logger.info(f"当前 URL: {self._page.url}")

    # 处理限流页面：如果被重定向到 rate-limit，则跳回购买页
    if "rate-limit" in self._page.url:
      logger.warning("检测到限流页面，跳回购买页...")
      await self._page.goto(self._purchase_url, wait_until="domcontentloaded")

    return self._page

  async def stop(self) -> None:
    """关闭浏览器并清理资源."""
    if self._context:
      await self._context.close()  # 关闭浏览器上下文
    if self._playwright:
      await self._playwright.stop()  # 停止 Playwright
    logger.info("浏览器已关闭")

  async def reload(self) -> None:
    """刷新当前页面."""
    if self._page:
      logger.info("刷新页面...")
      await self._page.reload(wait_until="domcontentloaded")

  async def navigate_to_purchase(self) -> None:
    """导航到购买页面（限流页面恢复用）."""
    if self._page:
      await self._page.goto(self._purchase_url, wait_until="domcontentloaded")

  # ==================== 抢购窗口判断 ====================

  def _is_in_rush_window(self) -> bool:
    """
    判断当前是否在抢购窗口内（10:00:00 - 10:02:00）.
    对应 JS 版的 isInRushWindow().
    在此窗口内的 API 响应会被修改（soldOut → false）.
    """
    now = datetime.now()
    target = now.replace(
        hour=self._target_hour,
        minute=self._target_minute,
        second=self._target_second,
        microsecond=0,  # 精确到秒
    )
    elapsed_ms = (now - target).total_seconds() * 1000  # 距目标时刻的毫秒数
    return 0 <= elapsed_ms < 120000  # 0 - 2分钟

  # ==================== 请求拦截（核心"欺骗"逻辑）====================

  async def _setup_routes(self) -> None:
    """
    设置 page.route() 网络拦截.
    拦截所有 /api/biz/pay/ 路径的请求和响应.
    """
    if not self._page:
      return

    # page.route(regex, handler): 每当浏览器发起匹配该正则的请求时，调用 handler
    await self._page.route(
        re.compile(r".*api/biz/pay/.*"),  # 匹配所有支付相关 API
        self._handle_api_route,            # 拦截处理函数
    )

  async def _setup_auth_capture(self) -> None:
    """
    监听页面发出的请求，从中捕获 Authorization 请求头.
    后续 api.py 直接调用 API 时需要这个 token.
    """
    if not self._page:
      return

    async def on_request(request):
      """每次页面发出请求时触发."""
      # 只在还未捕获到时执行
      if not self._auth_header:
        auth = request.headers.get("authorization", "")
        if auth:
          self._auth_header = auth
          logger.info("[捕获] Authorization 头已就绪")

    # 注册请求事件监听器
    self._page.on("request", on_request)

  async def _handle_api_route(self, route) -> None:
    """
    核心拦截处理函数 — 对每个匹配的 API 请求/响应进行修改.

    请求层面:
      - 请求头指纹随机化（X-Request-Id 等，防止被识别为脚本）
      - 注入缺失/错误的 productId

    响应层面:
      - 将 soldOut/isSoldOut 字段的 true 改为 false（仅在抢购窗口内）
      - 将 isServerBusy 字段的 true 改为 false
    """
    request = route.request
    url = request.url
    method = request.method

    # ========== 请求修改 ==========
    headers = dict(request.headers)

    # 请求头指纹随机化 — 每次请求看起来都不一样
    headers["X-Request-Id"] = uuid.uuid4().hex[:12]            # 随机 ID
    headers["X-Timestamp"] = str(random.randint(0, 9999999999)) # 随机时间戳
    q = round(0.5 + random.random() * 0.5, 1)                   # 随机质量值
    headers["Accept-Language"] = f"zh-CN,zh;q={q},en;q={round(q * 0.7, 1)}"

    body = request.post_data  # POST 请求体
    modified_body = None

    # productId 注入：处理 /preview/ 路径的订单预览请求
    if (
        "preview" in url          # 是 preview 请求
        and body                   # 有请求体
        and self._product_id       # 已有捕获的 productId
        and not self._force_pay_dialog_called  # 未触发支付弹窗
        and not self._order_created            # 订单未创建
    ):
      try:
        body_obj = json.loads(body)
        if not body_obj.get("productId"):
          # 情况1: 请求中缺少 productId → 注入
          body_obj["productId"] = self._product_id
          modified_body = json.dumps(body_obj)
          logger.info(f"[注入] 已补充 productId={self._product_id}")
        elif body_obj["productId"] != self._product_id:
          # 情况2: 请求中的 productId 与目标不一致 → 替换
          logger.info(
              f"[修正] productId 不符: "
              f'{body_obj["productId"]} → {self._product_id}'
          )
          body_obj["productId"] = self._product_id
          modified_body = json.dumps(body_obj)
      except (json.JSONDecodeError, Exception):
        pass  # JSON 解析失败，保持原样

    # ========== 发送请求 ==========
    try:
      if modified_body:
        # 有修改：用修改后的 body 重新发送请求
        response = await route.fetch(
            method=method,
            headers=headers,
            body=modified_body,
        )
      else:
        # 无修改：原样转发
        response = await route.fetch(
            method=method,
            headers=headers,
            body=body,
        )
    except Exception:
      # 请求失败：让浏览器继续正常流程
      await route.continue_()
      return

    # ========== 响应修改 ==========
    # 如果已确认售罄，不再修改响应
    if self._confirm_sold_out:
      await route.fulfill(response=response)
      return

    # 仅在抢购窗口内修改 soldOut 字段
    if not self._is_in_rush_window():
      await route.fulfill(response=response)
      return

    # 检查响应类型：只处理 JSON 和文本响应
    content_type = response.headers.get("content-type", "")
    if "json" in content_type or "text" in content_type:
      try:
        text = await response.text()  # 获取响应文本
        modified = self._modify_response_body(text, url)  # 修改售罄字段
        if modified != text:
          logger.debug("[拦截] 已修改响应中的售罄状态")
        # 用修改后的文本返回给页面
        await route.fulfill(
            response=response,
            body=modified,
        )
        return
      except Exception:
        pass  # 修改失败，继续原样返回

    # 不需要修改的响应：原样返回
    await route.fulfill(response=response)

  def _modify_response_body(self, text: str, url: str) -> str:
    """
    修改 API 响应文本中的售罄和服务器繁忙字段.

    使用正则替换将以下字段从 true 改为 false:
      - "isSoldOut": true
      - "soldOut": true
      - "is_sold_out": true
      - "sold_out": true
      - "isServerBusy": true

    仅处理与订单/产品相关的 URL（防止误伤其他 API）.
    """
    # 检查 URL 是否与订单/产品相关
    if not any(
        kw in url.lower()
        for kw in ["coding", "plan", "order", "subscribe", "product", "package"]
    ):
      return text  # 不相关则原样返回

    modified = text

    # 替换所有售罄字段：true → false
    for field in ["isSoldOut", "soldOut", "is_sold_out", "sold_out"]:
      modified = re.sub(
          rf'"{field}"\s*:\s*true',   # 匹配 "field": true（允许中间有空格）
          f'"{field}":false',          # 替换为 "field": false
          modified,
      )

    # 替换服务器繁忙字段
    modified = re.sub(
        r'"isServerBusy"\s*:\s*true',
        '"isServerBusy":false',
        modified,
    )

    return modified
