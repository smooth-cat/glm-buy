"""
调度器 — 整个项目的核心协调模块.

负责：
  1. 启动浏览器（通过 browser.py）
  2. 倒计时显示（终端实时更新）
  3. 到点触发抢购循环（读 DOM → 鼠标点击 → 检测弹窗 → 重复）
  4. 套餐售罄时的候补切换
  5. 页面异常时的自动恢复
  6. 验证码/支付弹窗的处理

对应 JS 版中的 startCountdown / startSnipe / tryNextPlan / setupAutoRetryRefresh 等函数.
"""

from __future__ import annotations

import asyncio
import time as time_module  # time.time() 用于精确计时
from datetime import datetime

from loguru import logger

from glm_buy.api import APIClient
from glm_buy.browser import BrowserManager
from glm_buy.config import PERIOD_LABELS, Config
from glm_buy.dom_reader import DOMReader
from glm_buy.mouse import Mouse
from glm_buy.notifier import async_notify, async_play_alert
from glm_buy.product import ProductManager
from glm_buy.verify.demo import solve_captcha
from glm_buy.verify.solver import CaptchaSolver

# 候补切换时，需要连续多少圈全部售罄才永久停止
SOLD_OUT_CYCLES_REQUIRED = 3


class Scheduler:
  """GLM 抢购的主调度器."""

  def __init__(self, config: Config) -> None:
    """初始化：创建各子模块，设定初始状态."""
    self.config = config
    self.product_mgr = ProductManager()    # 商品管理器
    self.api_client = APIClient()          # 直接 API 客户端
    self.browser: BrowserManager | None = None  # 浏览器管理器（start 时创建）
    self.dom: DOMReader | None = None      # DOM 读取器
    self.mouse: Mouse | None = None        # 鼠标模拟器

    # ---- 运行状态（对应 JS 版 state 对象） ----
    self._retry_count = 0        # 当前轮次重试次数
    self._is_running = False     # 是否正在抢购中
    self._order_created = False  # 订单是否已创建
    self._modal_visible = False  # 是否检测到弹窗（验证码/支付）
    self._preheated = False      # TCP 预热是否完成
    self._snipe_active = False   # 抢购循环是否活跃（用于安全退出循环）
    self._countdown_active = False  # 倒计时循环是否活跃

    # ---- 套餐切换管理 ----
    self._current_plan_idx = 0               # 当前正在尝试的套餐索引
    self._sold_out_in_cycle: set[str] = set()  # 本轮已售罄的套餐集合
    self._sold_out_cycle_count = 0           # 连续全售罄的圈数
    self._plan_switch_pending = False        # 套餐切换进行中（互斥锁）
    self._confirmed_sold_out = False         # 是否已确认售罄（永久停止）
    self._force_pay_dialog_called = False    # 是否已触发支付弹窗
    self._last_modal_type: str | None = None # 上次弹窗类型（captcha / payment）
    self._pid_fetch_attempt = 0             # productId 主动获取尝试次数

  # ==================== 便捷方法 ====================

  def _current_plan(self) -> str:
    """获取当前目标套餐名 ('lite'|'pro'|'max')."""
    return self.config.plan_priority[self._current_plan_idx].plan

  def _current_period(self) -> str:
    """获取当前目标计费周期 ('monthly'|'quarterly'|'yearly')."""
    return self.config.plan_priority[self._current_plan_idx].billing_period

  def _get_target_time(self) -> datetime:
    """
    获取下一个目标抢购时间.
    如果今天的目标时间已过，返回明天的目标时间.
    """
    cfg = self.config
    now = datetime.now()
    target = now.replace(
        hour=cfg.target_hour,
        minute=cfg.target_minute,
        second=cfg.target_second,
        microsecond=0,  # 精确到秒
    )
    if now >= target:
      # 今天的目标时间已过，推到明天
      from datetime import timedelta
      target += timedelta(days=1)
    return target

  def _is_near_target_time(self) -> bool:
    """
    判断当前是否接近目标时间（9:59 - 10:30 窗口）.
    在此窗口内才启动自动恢复和售罄探测.
    """
    now = datetime.now()
    target = now.replace(
        hour=self.config.target_hour,
        minute=self.config.target_minute,
        second=self.config.target_second,
        microsecond=0,
    )
    diff_ms = (target - now).total_seconds() * 1000
    return -1800000 <= diff_ms <= 60000  # -30min ~ +1min

  def _is_in_purchase_time(self) -> bool:
    """
    判断当前是否在抢购时间窗口内.
    从 [target - advanceMs] 开始，持续到 target + 30min.
    """
    now = datetime.now()
    target = now.replace(
        hour=self.config.target_hour,
        minute=self.config.target_minute,
        second=self.config.target_second,
        microsecond=0,
    )
    diff_ms = (target - now).total_seconds() * 1000
    return -1800000 <= diff_ms <= self.config.advance_ms

  def _is_in_rush_window(self) -> bool:
    """
    判断当前是否在强制拦截窗口内（10:00:00 - 10:02:00）.
    与 browser._is_in_rush_window() 逻辑相同，这里用于 schedule.
    """
    now = datetime.now()
    target = now.replace(
        hour=self.config.target_hour,
        minute=self.config.target_minute,
        second=self.config.target_second,
        microsecond=0,
    )
    elapsed_ms = (now - target).total_seconds() * 1000
    return 0 <= elapsed_ms < 120000

  # ==================== 套餐候补切换 ====================

  async def _try_next_plan(self) -> bool:
    """
    当前套餐售罄时，切换到优先级列表中的下一个候补套餐.
    与 JS 版 tryNextPlan() 完全对应.

    逻辑:
      1. 标记当前套餐在本轮已售罄
      2. 切换到下一个（到头则从头开始新一圈）
      3. 如果连续 N 圈全部售罄，确认永久售罄
      4. 否则重置 productId，重新开始抢购

    返回: True = 切换成功, False = 无法切换/已售罄.
    """
    if self._order_created:
      return False  # 订单已创建，不再切换

    # 互斥锁：防止并发切换
    if self._plan_switch_pending:
      logger.info("[候补] 切换进行中，跳过重复触发")
      return False

    self._plan_switch_pending = True  # 加锁
    try:
      # 1. 标记当前套餐在本轮已售罄
      self._sold_out_in_cycle.add(
          f"{self._current_plan()}_{self._current_period()}"
      )

      prev = self.config.plan_priority[self._current_plan_idx]
      self._current_plan_idx += 1  # 移到下一个

      if self._current_plan_idx >= len(self.config.plan_priority):
        # 走完一圈：检查是否全圈售罄
        if self._sold_out_in_cycle >= set(
            f"{p.plan}_{p.billing_period}"
            for p in self.config.plan_priority
        ):
          # 全售罄，增加圈数计数
          self._sold_out_cycle_count += 1
          if self._sold_out_cycle_count >= SOLD_OUT_CYCLES_REQUIRED:
            # 连续 N 圈全售罄 → 停止
            logger.info(
                f"[候补] 所有套餐已连续 {SOLD_OUT_CYCLES_REQUIRED} 圈售罄，停止抢购"
            )
            self._plan_switch_pending = False  # 提前解锁
            await self._confirm_sold_out_fn()
            return False
          logger.info(
              f"[候补] 全圈售罄 "
              f"(第 {self._sold_out_cycle_count}/{SOLD_OUT_CYCLES_REQUIRED} 圈)"
          )
        else:
          # 未全售罄（可能是波动），重置计数
          self._sold_out_cycle_count = 0
          first = self.config.plan_priority[0]
          logger.info(
              f"[候补] 轮询一圈未全售罄，重新从 {first.plan} 开始..."
          )
        # 从头开始新一圈
        self._current_plan_idx = 0
        self._sold_out_in_cycle.clear()

      nxt = self.config.plan_priority[self._current_plan_idx]
      is_same = prev.plan == nxt.plan and prev.billing_period == nxt.billing_period
      if not is_same:
        # 切换到了不同的套餐，日志和通知
        logger.info(
            f"[候补] {prev.plan}/{prev.billing_period} 售罄 → "
            f"切换到 {nxt.plan}/{nxt.billing_period}"
        )
        await async_notify(
            "GLM 候补切换",
            f"{prev.plan} 已售罄，正在尝试 {nxt.plan}",
        )

      # 2. 重置状态：清空旧 productId，用新套餐重新获取
      self.product_mgr.captured_product_id = None
      self.product_mgr.get_product_id(
          self._current_plan(), self._current_period()
      )
      self._retry_count = 0  # 给新套餐完整的重试次数

      # 3. 如果还在抢购窗口内，立即重新开始抢购
      if self._is_in_purchase_time():
        self._is_running = True
        await self._start_snipe()

      return True
    except Exception as e:
      logger.error(f"[候补] 切换异常: {e}")
      return False
    finally:
      self._plan_switch_pending = False  # 解锁

  async def _confirm_sold_out_fn(self) -> None:
    """
    确认售罄 → 停止一切抢购逻辑.
    通知 scheduler 和 browser 进入售罄状态.
    """
    if self._confirmed_sold_out:
      return  # 防止重复调用
    self._confirmed_sold_out = True
    if self.browser:
      self.browser.confirm_sold_out = True  # 同步到浏览器（停止响应修改）
    logger.info("已确认售罄，停止抢购")
    self._is_running = False
    await async_notify("GLM 抢购失败", "今日已售罄，明天 10:00 再来！")

  # ==================== 倒计时 ====================

  async def _run_countdown(self) -> None:
    """
    倒计时循环 — 每 0.5 秒更新一次终端显示.

    在不同时间点触发不同动作:
      T-60s:   预热（直接调用 API 获取 productId）
      T-10s:   自动刷新页面（获取最新状态）
      T-3s:    TCP 预热
      T-200ms: 开始抢购循环

    终端显示格式:
      距目标 > 60s:  "HH:MM:SS"（白色/黄色）
      距目标 <= 60s: "S.SSSs"（红色毫秒倒计时）
      抢购中:        "抢购中..."（绿色）
      已售罄:        "已售罄"（红色）
    """
    logger.info("倒计时已启动")
    prewarm_done = False  # 预热是否已完成

    self._countdown_active = True
    while self._countdown_active:
      target = self._get_target_time()
      diff_ms = (target - datetime.now()).total_seconds() * 1000

      # 已过目标时间 → 退出倒计时（应由其他逻辑进入抢购）
      if diff_ms <= 0:
        logger.info("已过目标时间")
        break

      # 计算时/分/秒/毫秒
      hours = int(diff_ms // 3600000)
      minutes = int((diff_ms % 3600000) // 60000)
      seconds = int((diff_ms % 60000) // 1000)

      # ---- 终端倒计时显示 ----
      if self._is_running:
        status = "抢购中..."
      elif diff_ms <= 60000:
        status = f"{seconds}.{int((diff_ms % 1000)):03d}s"  # 毫秒精度
      elif self._confirmed_sold_out:
        status = "已售罄"
      else:
        status = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

      plan_name = self._current_plan().upper()
      period_label = PERIOD_LABELS.get(self._current_period(), "包季")
      # \r 回到行首，实现原地更新
      print(
          f"\r  ⏱ {status} | 目标: {plan_name} / {period_label}    ",
          end="",
          flush=True,
      )

      # ---- T-10s: 自动刷新页面 ----
      if (
          self.config.auto_refresh
          and not self._is_running
          and diff_ms <= self.config.auto_refresh_seconds_before * 1000
          and diff_ms > (self.config.auto_refresh_seconds_before - 1) * 1000
      ):
        logger.info("自动刷新页面以获取最新状态...")
        if self.browser:
          await self.browser.reload()

      # ---- T-60s: 预热（直接 API 获取 productId）----
      if diff_ms <= 60000 and not prewarm_done and not self._is_running:
        prewarm_done = True
        logger.info("预热: 直接捕获 productId...")
        await self._fetch_product_id_directly()

      # ---- T-3s: TCP 预热（浏览器已维护连接，此处仅做占位记录）----
      if diff_ms <= 3000 and not self._preheated:
        self._preheated = True
        logger.debug("TCP 预热（浏览器已维护连接池）")

      # ---- 到点：开始抢购 ----
      if diff_ms <= self.config.advance_ms and not self._is_running:
        self._is_running = True
        logger.info(f"开始抢购! (提前{self.config.advance_ms}ms)")
        await self._start_snipe()
        break  # 退出倒计时循环

      await asyncio.sleep(0.5)  # 每 0.5 秒更新一次

  # ==================== 核心抢购循环 ====================

  async def _start_snipe(self) -> None:
    """
    主抢购循环 — 对应 JS 版 startSnipe().

    流程:
      1. 点击计费周期标签（触发 API 请求获取 productId）
      2. 等待/获取 productId
      3. 进入重试循环（最多 max_retries 次）:
         a. 查找购买按钮 → 鼠标点击
         b. 查找确认按钮 → 鼠标点击
         c. 检测弹窗（验证码 → 暂停通知用户，支付 → 成功停止）
         d. 检测二维码（抢购成功）
    """
    # 已确认售罄，不启动
    if self._confirmed_sold_out:
      logger.info("已确认售罄，不启动抢购")
      self._is_running = False
      return

    # 确保各模块已初始化
    if not self.dom or not self.mouse or not self.browser:
      logger.error("浏览器未初始化")
      return

    # 第一步：选择计费周期（点击"包季"/"包月"标签）
    # 这会触发页面的 API 调用，从而捕获 productId
    await self._select_billing_period()

    # 第二步：确保 productId 就绪
    if not self.product_mgr.get_product_id(
        self._current_plan(), self._current_period()
    ):
      logger.info("productId 未就绪，等待 batch-preview 响应...")
      # 等待最多 3 秒
      await self._wait_for_product_id(3000)
      if not self.product_mgr.captured_product_id:
        # 3秒还没拿到，主动调用 API 获取
        logger.info("主动获取 productId...")
        await self._fetch_product_id_directly()

    # 将 productId 同步到浏览器（用于请求拦截中的注入）
    self.browser.product_id = self.product_mgr.captured_product_id

    if not self.product_mgr.captured_product_id:
      logger.warning("productId 仍未获取到，将在循环中等待")

    logger.info(f"productId: {self.product_mgr.captured_product_id or '未获取'}")

    # 第三步：主点击循环
    waiting_for_pid = False  # 是否正在等待 productId（避免重复日志）
    self._retry_count = 0

    self._snipe_active = True
    try:
      while self._snipe_active:
        # ---- 退出条件检查 ----
        if self._confirmed_sold_out:
          logger.info("已确认售罄，停止抢购")
          break

        if self._order_created:
          break  # 订单已创建（检测到支付二维码）

        if self._retry_count >= self.config.max_retries:
          logger.info("本轮重试结束，等待页面恢复后重新触发...")
          self._is_running = False
          self._retry_count = 0
          break

        # ---- productId 未就绪时的处理 ----
        if not self.product_mgr.captured_product_id:
          if not waiting_for_pid:
            waiting_for_pid = True
            logger.info("productId 未就绪，暂停点击，等待后台获取...")
          await asyncio.sleep(self.config.retry_interval / 1000)
          continue  # 跳过本轮，不消耗重试次数
        waiting_for_pid = False

        self._retry_count += 1
        if self._retry_count % 10 == 1:
          # 每10次打一次日志，减少刷屏
          logger.info(f"第 {self._retry_count} 次尝试...")

        # ---- 1. 查找并点击购买按钮 ----
        coords = await self.dom.find_purchase_button(self._current_plan())
        if coords:
          await self.mouse.click(coords[0], coords[1])
          logger.info("已点击购买按钮!")

        # ---- 2. 查找并点击确认按钮（弹窗中的"立即支付"等）----
        confirm_coords = await self.dom.find_confirm_button()
        if confirm_coords:
          await self.mouse.click(confirm_coords[0], confirm_coords[1])

        # ---- 3. 检测弹窗 ----
        modal_type = await self.dom.detect_modal()
        if modal_type:
          if not self._modal_visible:
            self._modal_visible = True
            self._last_modal_type = modal_type
            label = "验证码" if modal_type == "captcha" else "支付"
            logger.info(f"检测到{label}弹窗! 冻结刷新")
            await async_play_alert()

            if modal_type == "captcha":
              # 自动识别 + 点击验证码
              logger.info("开始自动识别验证码...")
              captcha_solver = CaptchaSolver()
              ok = await solve_captcha(self.browser.page, self.mouse, captcha_solver)
              if ok:
                logger.info("验证码已自动通过，继续抢购...")
                self._modal_visible = False
                continue
              logger.warning("验证码自动识别失败，等待手动处理...")
              await async_notify("GLM 需要验证", "请手动完成验证码")

          break  # 支付弹窗 → 停止点击循环

        elif self._modal_visible:
          # 弹窗消失了
          self._modal_visible = False
          logger.info("弹窗已消失，恢复自动抢购")

          # 如果是验证码弹窗消失，4秒后再检查支付弹窗是否出现
          if (
              self._last_modal_type == "captcha"
              and not self._order_created
              and self._is_in_purchase_time()
          ):
            await self._post_captcha_retry()

          self._last_modal_type = None

        # ---- 4. 检测支付二维码（抢购成功标志）----
        if await self.dom.detect_qr_code():
          logger.info("检测到支付二维码!")
          self._order_created = True
          if self.browser:
            self.browser.order_created = True  # 同步到浏览器
          await async_play_alert()             # 播放提示音
          await async_notify(
              "GLM 抢购成功！",
              "支付二维码已出现，快去扫码！",
          )
          break  # 退出抢购循环

        # 等待 retry_interval 毫秒后继续下一轮
        await asyncio.sleep(self.config.retry_interval / 1000)
    finally:
      self._snipe_active = False  # 确保循环退出

  async def _select_billing_period(self) -> None:
    """
    点击当前套餐的计费周期标签（包月/包季/包年）.
    对应 JS 版 selectBillingPeriod().
    """
    if not self.dom or not self.mouse:
      return

    coords = await self.dom.find_billing_period_tab(
        self._current_plan(), self._current_period()
    )
    if coords:
      await self.mouse.click(coords[0], coords[1])

  async def _post_captcha_retry(self) -> None:
    """
    验证码通过后的恢复逻辑.
    等待 4 秒 → 检查支付弹窗是否出现 → 若未出现则重新开始购买流程.

    这是必需的，因为验证码完成后页面状态可能已变化（productId 失效等），
    需要重新获取 productId 并重新点击购买按钮.
    """
    await asyncio.sleep(4)  # 等 4 秒让页面完成验证码后的状态更新
    if self._order_created or self._confirmed_sold_out:
      return

    logger.info("[验证码后] 支付弹窗未出现，重新获取 productId...")
    # 清掉旧的 productId（验证码过程中可能已失效）
    self.product_mgr.captured_product_id = None
    # 重新获取
    await self._fetch_product_id_directly()
    await asyncio.sleep(1)  # 等 1 秒让页面消化新的数据

    if self._order_created or self._confirmed_sold_out:
      return

    if not self.product_mgr.captured_product_id:
      logger.warning("[验证码后] productId 仍未获取到，放弃本轮重试")
      return

    logger.info("[验证码后] productId 就绪，重新触发购买...")
    # 重置状态，重新开始抢购
    self._retry_count = 0
    self._force_pay_dialog_called = False
    if self.browser:
      self.browser.force_pay_dialog_called = False
    self._is_running = True
    await self._start_snipe()  # 递归调用（每次递归都有完整的状态重置）

  # ==================== Product ID 辅助获取 ====================

  async def _wait_for_product_id(self, max_wait_ms: int = 3000) -> bool:
    """
    轮询等待 productId 就绪（每 100ms 检查一次）.
    对应 JS 版 waitForProductId().
    """
    start = time_module.monotonic()
    while (
        time_module.monotonic() - start
    ) * 1000 < max_wait_ms:
      if self.product_mgr.get_product_id(
          self._current_plan(), self._current_period()
      ):
        return True
      await asyncio.sleep(0.1)  # 每 100ms 检查一次
    return False

  async def _fetch_product_id_directly(self) -> bool:
    """
    绕过浏览器，直接调用 API 获取 productId.
    对应 JS 版 fetchProductIdDirectly().

    策略:
      1. 先调 batch-preview API
      2. 失败则调 productinfo API 兜底

    需要先有 Authorization 头（从浏览器请求中捕获）.
    """
    if self.product_mgr.captured_product_id:
      return True  # 已经有了，不需要

    # 必须拿到 Authorization 头才能调 API
    auth = self.browser.auth_header if self.browser else None
    if not auth:
      logger.info("[主动获取] Authorization 头未就绪，跳过")
      return False

    # 同步 Authorization 到 API 客户端
    self.api_client.auth_header = auth

    # 尝试 batch-preview
    logger.info("[主动获取] 尝试直接调用产品列表 API...")
    data = self.api_client.batch_preview()
    if data:
      self.product_mgr.capture_product_id_from_data(
          data.get("data", data),  # 兼容 data 嵌套
          self._current_plan(),
          self._current_period(),
      )
      if self.product_mgr.captured_product_id:
        logger.info(
            f"[主动获取] 成功: productId={self.product_mgr.captured_product_id}"
        )
        return True

    # 兜底：productinfo API
    logger.info("[主动获取] batch-preview 失败，尝试 productinfo API...")
    data2 = self.api_client.product_info()
    if data2:
      self.product_mgr.capture_product_id_from_data(
          data2.get("data", data2),
          self._current_plan(),
          self._current_period(),
      )
      if self.product_mgr.captured_product_id:
        logger.info(
            f"[主动获取] productinfo 成功: "
            f"productId={self.product_mgr.captured_product_id}"
        )
        return True

    logger.info("[主动获取] 所有 API 均未返回匹配的 productId")
    return False

  # ==================== 售罄探测 ====================

  async def probe_sold_out_status(self) -> None:
    """
    绕过浏览器拦截，直接调 API 探测服务端真实售罄状态.
    对应 JS 版 probeSoldOutStatus().

    在抢购窗口结束后（10:02 后）调用，因为此时 page.route() 已停止拦截，
    API 返回的是真实数据（不再伪装 soldOut=false）.
    """
    if self._order_created or self._confirmed_sold_out:
      return  # 订单已创建或已确认售罄，无需探测

    logger.info("[探测] 主动同步服务端 soldOut 状态...")
    data = self.api_client.batch_preview()
    if not data:
      logger.info("[探测] HTTP 请求失败，跳过")
      return

    # 提取 productList（兼容嵌套 data）
    product_list = data.get("data", {}).get("productList", []) or data.get(
        "productList", []
    )
    logger.info(
        f"[探测] productList 共 {len(product_list)} 条: "
        f'{", ".join(f"{i.get("monthlyOriginalAmount")}/{i.get("soldOut", i.get("isSoldOut"))}" for i in product_list)}'
    )

    # 构建"已配置套餐"集合
    configured_keys = {
        f"{p.plan}_{p.billing_period}" for p in self.config.plan_priority
    }
    # 解析 productList，找出每个已配置套餐的售罄状态
    sold_out_map: dict[str, bool] = {}
    for item in product_list:
      if not isinstance(item, dict):
        continue
      info = self.product_mgr.identify_plan_from_product(item)
      if info is None:
        continue
      key = f"{info.plan}_{info.period}"
      if key not in configured_keys or key in sold_out_map:
        continue
      sold_out_map[key] = (
          item.get("soldOut") is True or item.get("isSoldOut") is True
      )

    current_key = f"{self._current_plan()}_{self._current_period()}"
    # 判断：全部售罄 / 当前售罄 / 有货
    all_sold_out = configured_keys and all(
        sold_out_map.get(k) is True for k in configured_keys
    )
    current_sold_out = sold_out_map.get(current_key)

    if all_sold_out:
      logger.info(f"[探测] 所有已配置套餐均售罄 ({', '.join(configured_keys)})，停止抢购")
      await self._confirm_sold_out_fn()
    elif current_sold_out is True:
      logger.info(f"[探测] 服务端确认 {current_key} 售罄，停止抢购")
      await self._confirm_sold_out_fn()
    elif current_sold_out is False:
      logger.info(f"[探测] 服务端确认 {current_key} 有货，继续抢购")
    else:
      logger.info(f"[探测] 未找到当前套餐 {current_key} 的数据，忽略")

  # ==================== 自动恢复 ====================

  async def auto_recovery_check(self) -> None:
    """
    定期检查页面状态，发现异常自动恢复.
    对应 JS 版 setupAutoRetryRefresh().

    检查项:
      - rate-limit 页面 → 跳回购买页
      - 页面错误/空白 → 刷新页面
    """
    if not self.dom or not self.browser:
      return
    if not self._is_near_target_time():  # 只在接近目标时间时检查
      return
    if self._modal_visible:  # 有弹窗时不刷新（保护验证码/支付弹窗）
      return

    if await self.dom.is_rate_limit_page():
      logger.warning("检测到限流页，跳回购买页...")
      await self.browser.navigate_to_purchase()
      return

    if await self.dom.has_error() or await self.dom.is_page_blank():
      logger.warning("页面异常，尝试刷新...")
      await self.browser.reload()

  async def auto_snipe_on_ready(self) -> None:
    """
    定期检查：如果页面已恢复且有购买按钮，自动重新触发抢购.
    对应 JS 版 setupAutoSnipeOnReady().
    """
    if not self.dom:
      return
    if not self._is_in_purchase_time():  # 不在抢购窗口
      return
    if (
        self._is_running          # 已在运行
        or self._order_created    # 订单已创建
        or self._modal_visible    # 有弹窗
        or self._confirmed_sold_out  # 已确认售罄
        or self._plan_switch_pending # 正在切换套餐
    ):
      return  # 不满足触发条件

    if await self.dom.has_error():
      return  # 页面仍有错误

    # 检查页面是否有可用的购买按钮
    if await self.dom.has_any_purchase_button():
      logger.info("页面恢复正常，自动触发抢购!")
      self._is_running = True
      await self._start_snipe()

  # ==================== 主入口 ====================

  async def run(self) -> None:
    """
    主入口 — 启动浏览器 → 加载缓存 → 倒计时 → 抢购循环.

    整体流程:
      1. 启动浏览器
      2. 初始化 DOM / 鼠标模块
      3. 加载本地缓存的 productId
      4. 校准服务器时间（延迟 2s 执行）
      5. 启动定期 productId 检查（每 3s）
      6. 如果当前正在抢购窗口内 → 直接开始抢购
      7. 否则 → 进入倒计时循环，到点自动抢购
      8. 循环：倒计时 → 抢购 → 恢复检查 → 售罄探测 → 等待明天
    """

    # 1. 启动浏览器
    self.browser = BrowserManager(
        headless=self.config.headless,
        user_data_dir=self.config.user_data_dir,
        purchase_url=self.config.purchase_url,
        target_hour=self.config.target_hour,
        target_minute=self.config.target_minute,
        target_second=self.config.target_second,
    )
    page = await self.browser.start()  # 启动浏览器并打开购买页面

    # 2. 初始化 DOM 读取器和鼠标模拟器
    self.dom = DOMReader(page)
    self.mouse = Mouse(page)

    # 3. 延迟 2 秒校准服务器时间（不阻塞主流程）
    asyncio.create_task(self._delayed_time_calibration())

    # 4. 从本地缓存加载 productId
    pid = self.product_mgr.get_product_id(
        self._current_plan(), self._current_period()
    )
    if pid:
      logger.info(f"[本地缓存] 预加载 productId={pid}")
      self.browser.product_id = pid  # 同步到浏览器

    # 5. 启动定期 productId 检查（每 3s 一次）
    self._pid_check_task = asyncio.create_task(self._periodic_pid_check())

    # 6. 如果当前已经在抢购窗口内，延迟 2 秒后直接开始抢购
    if self._is_in_purchase_time():
      await asyncio.sleep(2)  # 等待页面数据加载
      if not self._confirmed_sold_out:
        logger.info("当前正是抢购时间! 立即开始!")
        self._is_running = True
        await self._start_snipe()
        return  # 抢购完成后返回（run 函数只在到点瞬间运行一次核心战斗）

    # 7. 打印配置信息
    plan_list = "，".join(
        f"{'首选' if i == 0 else '候补' + str(i)}: "
        f"{p.plan}/{p.billing_period}"
        for i, p in enumerate(self.config.plan_priority)
    )
    logger.info(f"脚本已启动 - {plan_list}")
    logger.info(
        f"抢购时间: 每天 "
        f"{self.config.target_hour}:"
        f"{self.config.target_minute:02d}:"
        f"{self.config.target_second:02d}"
    )
    logger.info("提前10秒自动刷新，到点自动抢购")

    # 8. 主循环：倒计时 → 抢购 → 恢复 → 探测 → 等待下一轮
    while True:
      # 8a. 倒计时（阻塞直到开始抢购）
      await self._run_countdown()

      # 8b. 抢购结束后的 2 分钟维护期：自动恢复 + 自动重触发
      for _ in range(60):  # 60 × 2s = 2分钟
        if self._order_created or self._confirmed_sold_out:
          break  # 订单完成或售罄，退出恢复检查
        await asyncio.sleep(2)
        await self.auto_recovery_check()
        await self.auto_snipe_on_ready()

      # 8c. 窗口结束后探测服务端真实售罄状态
      await self.probe_sold_out_status()

      # 8d. 如果订单已创建或确认售罄，等待到明天
      if self._order_created or self._confirmed_sold_out:
        logger.info("本轮抢购结束，等待明天...")
        wait_s = max(60, (self._get_target_time() -
                     datetime.now()).total_seconds())
        # 重置所有状态，准备明天的战斗
        self._confirmed_sold_out = False
        self._sold_out_cycle_count = 0
        self._sold_out_in_cycle.clear()
        self._current_plan_idx = 0
        self._force_pay_dialog_called = False
        self._order_created = False
        self._is_running = False
        self._retry_count = 0
        if self.browser:
          self.browser.confirm_sold_out = False
          self.browser.order_created = False
          self.browser.force_pay_dialog_called = False
        logger.info(f"等待 {wait_s:.0f}s 到下一轮...")
        await asyncio.sleep(wait_s)

  async def _delayed_time_calibration(self) -> None:
    """延迟 2 秒后校准服务器时间（避免阻塞启动）."""
    await asyncio.sleep(2)
    self.api_client.calibrate_time()

  async def _periodic_pid_check(self) -> None:
    """
    后台定期检查 productId 是否已获取.
    每 3 秒检查一次内存缓存，每 6 秒主动调一次 API.
    一旦获取到就停止.
    """
    while self.browser and self.browser.page:
      await asyncio.sleep(3)
      if self.product_mgr.captured_product_id:
        continue  # 已经有了，跳过

      # 先尝试从内存/文件缓存获取
      if self.product_mgr.get_product_id(
          self._current_plan(), self._current_period()
      ):
        if self.browser:
          self.browser.product_id = self.product_mgr.captured_product_id
        continue

      # 每 2 次（6 秒）主动调一次 API
      self._pid_fetch_attempt += 1
      if self._pid_fetch_attempt % 2 == 0:
        await self._fetch_product_id_directly()
        if self.browser:
          self.browser.product_id = self.product_mgr.captured_product_id
