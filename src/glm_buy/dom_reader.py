"""
DOM 读取与判断模块 — 只读，绝不修改 DOM.

对应 JS 版的 tryClickPurchaseButton / tryClickConfirmButton / selectBillingPeriod /
detectModal / detectQRCode 等函数.

核心流程：
  - 查找页面元素（按钮、弹窗、二维码等）→ 获取其坐标（boundingBox）
  - 将坐标传给 mouse.py 执行鼠标移动 + 点击
  - 绝不使用 element.click() 等 DOM API

为什么只读？
  用户要求"所有操作必须模拟用户鼠标"，即不能通过 JS/DOM API 触发事件。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from glm_buy.config import PLAN_KEYWORDS  # 各套餐在页面上的文本关键词

# TYPE_CHECKING 为 True 只在编辑器/类型检查时，运行时不导入（避免循环引用）
if TYPE_CHECKING:
  from playwright.async_api import Page, Locator, ElementHandle


# ==================== 关键词常量 ====================

# 购买按钮可能包含的文本（中文 + 英文）
BUY_KEYWORDS = [
    "特惠订阅", "特惠购买", "立即购买", "立即订阅", "立即订购",
    "购买", "订阅", "订购",
    "Subscribe", "Buy", "Purchase",
]

# 确认/支付按钮可能包含的文本
CONFIRM_KEYWORDS = [
    "确认", "确定", "立即支付", "去支付", "提交订单",
    "Confirm", "OK", "Submit",
]

# 售罄关键词（发现这些文本则跳过此按钮）
SOLD_OUT_KEYWORDS = ["售罄", "补货", "sold out", "Sold Out"]

# 页面错误关键词（页面加载失败/服务器繁忙）
ERROR_KEYWORDS = [
    "访问人数较多", "请刷新重试", "请稍后再试",
    "服务繁忙", "网络错误", "加载失败",
]

# 验证码弹窗关键词
CAPTCHA_KEYWORDS = ["验证", "滑动", "拖动"]


class DOMReader:
  """
  只读 DOM 检查器.
  提供方法查找购买按钮、确认按钮、计费周期标签、弹窗类型等.
  所有方法返回坐标或布尔值，不修改 DOM.
  """

  def __init__(self, page: Page) -> None:
    """初始化，page 是 Playwright 的 Page 对象."""
    self._page = page
    # 记录初始 QR 数量，只检测增量。-1 表示首次调用待建基线
    self._last_qr_count = -1

  # ==================== 页面状态检测 ====================

  async def has_error(self) -> bool:
    """
    检查页面是否显示错误文本（服务器繁忙等）.
    用于自动恢复逻辑（检测到错误 → 刷新页面）.
    """
    try:
      text = await self._page.inner_text("body") or ""
      return any(kw in text for kw in ERROR_KEYWORDS)
    except Exception:
      return False

  async def is_rate_limit_page(self) -> bool:
    """检查当前是否被重定向到了限流页面."""
    url = self._page.url
    return "rate-limit" in url

  async def is_page_blank(self) -> bool:
    """
    检查页面是否为空（HTML 加载失败）.
    判断标准：body 子元素 < 3 且文本内容 < 100 字符.
    """
    try:
      child_count = await self._page.evaluate(
          "document.body ? document.body.children.length : 0"
      )
      text = await self._page.inner_text("body") or ""
      return child_count < 3 and len(text.strip()) < 100
    except Exception:
      return True  # 读取失败也认为页面有问题

  # ==================== 按钮查找 ====================

  async def find_purchase_button(
      self, plan: str
  ) -> tuple[float, float] | None:
    """
    查找指定套餐的购买按钮，返回按钮中心坐标 (x, y).

    查找策略（与 JS 版 tryClickPurchaseButton 对应）:
      策略1: 先找到套餐卡片区域 → 在区域内找包含"购买"文字的按钮
      策略2: 通过 data-plan / id 等属性选择器查找

    返回: 按钮中心坐标，找不到返回 None.
    """
    plan_keys = PLAN_KEYWORDS.get(plan, [])  # 如 ["lite", "LITE", "基础"]

    # ---- 策略1: 文本匹配 ----
    # 先找到页面中所有可能的"卡片"元素（div/section/article/li）
    section_elements = await self._page.locator(
        "div, section, article, li"
    ).all()

    target_section = None
    for el in section_elements:
      try:
        text = await el.text_content() or ""
        # 包含套餐关键词（如 "LITE"）
        if any(k in text for k in plan_keys):
          box = await el.bounding_box()  # 获取元素位置和尺寸
          # 排除太大的元素（整个页面），只保留卡片大小的（高<800, 宽<600）
          if box and box["height"] < 800 and box["width"] < 600:
            target_section = el
            break  # 找到第一个匹配的卡片就停止
      except Exception:
        continue

    # 搜索范围：如果找到了目标卡片就只在其内部搜，否则全页面搜
    search_root = target_section or self._page

    # 在搜索范围内找所有按钮类元素
    buttons = await search_root.locator(
        'button, a[role="button"], [class*="btn"], [class*="button"]'
    ).all()

    for btn in buttons:
      try:
        text = await btn.text_content() or ""
        text = text.strip()
        # 跳过包含"售罄"的按钮
        if any(kw in text for kw in SOLD_OUT_KEYWORDS):
          continue
        # 必须是购买按钮，且在目标卡片内或附近有 plan 文本
        if any(kw in text for kw in BUY_KEYWORDS):
          if target_section or await self._has_nearby_plan_text(btn, plan_keys):
            if await self._is_visible(btn):  # 确保可见
              box = await btn.bounding_box()
              if box:
                # 返回按钮中心坐标
                return (
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
      except Exception:
        continue

    # ---- 策略2: data 属性 / ID 选择器 ----
    specific_selectors = [
        f'[data-plan="{plan}"]',
        f'[data-type="{plan}"]',
        f"#{plan}-buy-btn",
        f"#buy-{plan}",
        f".{plan}-purchase",
        f'[data-plan-type="{plan}"]',
    ]
    for sel in specific_selectors:
      try:
        el = self._page.locator(sel).first  # 取第一个匹配元素
        # count() > 0 表示存在，is_visible() 表示可见
        if await el.count() > 0 and await self._is_visible(el):
          box = await el.bounding_box()
          if box:
            return (
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
            )
      except Exception:
        continue

    return None

  async def find_confirm_button(self) -> tuple[float, float] | None:
    """
    查找弹窗中的确认/支付按钮，返回坐标.
    只在可见的模态框/对话框中搜索.
    """
    # 找到所有模态框/对话框元素
    modals = await self._page.locator(
        '[class*="modal"], [class*="dialog"], [class*="popup"], '
        '[class*="overlay"], [role="dialog"]'
    ).all()

    for modal in modals:
      if not await self._is_visible(modal):
        continue  # 不可见的跳过

      # 在弹窗内找所有按钮
      buttons = await modal.locator(
          'button, a[role="button"]'
      ).all()
      for btn in buttons:
        try:
          text = await btn.text_content() or ""
          text = text.strip()
          if any(kw in text for kw in CONFIRM_KEYWORDS):
            box = await btn.bounding_box()
            if box:
              logger.info(f'点击确认按钮: "{text}"')
              return (
                  box["x"] + box["width"] / 2,
                  box["y"] + box["height"] / 2,
              )
        except Exception:
          continue
    return None

  async def find_billing_period_tab(
      self, plan: str, period: str
  ) -> tuple[float, float] | None:
    """
    查找计费周期标签（包月/包季/包年），返回坐标.

    与 JS 版 selectBillingPeriod() 对应.
    匹配规则（来自 config.PERIOD_TAB_KEYWORDS）:
      monthly:    包含"包月"，排除"包季""包年"
      quarterly:  包含"包季"，排除"包月""包年"
      yearly:     包含"包年"，排除"包月""包季"
    """
    from glm_buy.config import PERIOD_TAB_KEYWORDS  # 延迟导入

    period_cfg = PERIOD_TAB_KEYWORDS.get(period)
    if not period_cfg:
      return None  # 无效的 period 参数

    match_kw = period_cfg["match"]      # 必须包含的关键词
    exclude_kws = period_cfg["exclude"]  # 不能包含的关键词

    # 先找到套餐卡片以缩小搜索范围
    plan_keys = PLAN_KEYWORDS.get(plan, [])
    search_root = self._page  # 默认全页面搜索
    section_elements = await self._page.locator(
        "div, section, article, li"
    ).all()
    for el in section_elements:
      try:
        text = await el.text_content() or ""
        if any(k in text for k in plan_keys):
          box = await el.bounding_box()
          if box and box["height"] < 800 and box["width"] < 600:
            search_root = el  # 在目标卡片内搜索
            break
      except Exception:
        continue

    # 在搜索范围内找所有可能的标签元素
    tabs = await search_root.locator(
        "div, span, button, a, li, label"
    ).all()
    for tab in tabs:
      try:
        text = await tab.text_content() or ""
        text = text.strip()
        # 必须包含 match_kw，不能含有 exclude_kws，且文本长度<20
        if (
            match_kw in text
            and all(ex not in text for ex in exclude_kws)
            and len(text) < 20
        ):
          box = await tab.bounding_box()
          if box:
            logger.info(f"已选择: {period_cfg['label']}")
            return (
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
            )
      except Exception:
        continue

    logger.info(f"未找到{period_cfg['label']}选项，使用页面默认")
    return None

  # ==================== 弹窗检测 ====================

  async def detect_modal(self) -> str | None:
    """
    检测当前页面是否存在验证码或支付弹窗.
    返回: 'captcha' = 验证码弹窗, 'payment' = 支付弹窗, None = 无弹窗.

    判断依据:
      - captcha: 弹窗文本包含"验证"/"滑动"/"拖动"，或内部有 captcha/verify/slider- 类元素
      - payment: 弹窗文本包含"扫码"/"支付"/"付款"，或内部有 canvas/qr 图片
    """
    modals = await self._page.locator(
        '[class*="modal"], [class*="dialog"], [class*="popup"], [role="dialog"]'
    ).all()

    for modal in modals:
      # 跳过不可见的弹窗
      if not await self._is_visible(modal):
        continue
      # 跳过太小的弹窗（高度 < 30px 的不算真正弹窗）
      if await self._modal_height(modal) < 30:
        continue

      text = await modal.text_content() or ""

      # ---- 检查是否为验证码弹窗 ----
      has_captcha = any(kw in text for kw in CAPTCHA_KEYWORDS)
      if not has_captcha:
        try:
          els = await modal.locator(
              '[class*="captcha"], [class*="verify"], [class*="slider-"]'
          ).all()
          has_captcha = len(els) > 0
          if has_captcha:
            tags = [await e.evaluate("el => el.tagName + (el.className ? '.' + el.className.split(' ')[0] : '')") for e in els[:5]]
            logger.info(f"验证码子元素: {tags}")
        except Exception:
          pass

      # ---- 检查是否为支付弹窗 ----
      has_payment = any(kw in text for kw in ["扫码", "支付", "付款"])
      if not has_payment:
        try:
          els = await modal.locator(
              'canvas, img[src*="qr"], img[src*="pay"]'
          ).all()
          has_payment = len(els) > 0
          if has_payment:
            tags = [await e.evaluate("el => '<' + el.tagName.toLowerCase() + '>' + (el.outerHTML || '').slice(0, 80)") for e in els[:5]]
            logger.info(f"支付子元素: {tags}")
        except Exception:
          pass

      if has_captcha:
        return "captcha"
      if has_payment:
        return "payment"

    return None  # 没有检测到任何弹窗

  async def detect_qr_code(self) -> bool:
    """
    检测可见弹窗内是否有新的支付二维码出现（仅检测增量，对应 JS 版 MutationObserver）.
    搜索范围限定为可见弹窗，与 detect_modal 一致.
    """
    try:
      # 只在可见弹窗内搜索（与 detect_modal 相同范围）
      modals = await self._page.locator(
          '[class*="modal"], [class*="dialog"], [class*="popup"], [role="dialog"]'
      ).all()
      total = 0
      for modal in modals:
        if not await self._is_visible(modal):
          continue
        total += await modal.locator(
            'canvas, img[src*="qr"], img[src*="pay"]'
        ).count()

      if self._last_qr_count == -1:
        self._last_qr_count = total
        return False
      if total > self._last_qr_count:
        self._last_qr_count = total
        return True
      return False
    except Exception:
      return False

  async def has_qr_code(self) -> bool:
    """
    检查可见弹窗内是否存在支付二维码（绝对检查，非增量）.
    用于支付弹窗分支，不依赖 _last_qr_count 基线.
    """
    try:
      modals = await self._page.locator(
          '[class*="modal"], [class*="dialog"], [class*="popup"], [role="dialog"]'
      ).all()
      for modal in modals:
        if not await self._is_visible(modal):
          continue
        if await modal.locator(
            'canvas, img[src*="qr"], img[src*="pay"]'
        ).count() > 0:
          return True
      return False
    except Exception:
      return False

  # ==================== 辅助方法 ====================

  async def _is_visible(self, el: Locator | ElementHandle) -> bool:
    """检查元素是否在页面上可见."""
    try:
      return await el.is_visible()
    except Exception:
      return False

  async def _modal_height(self, modal) -> float:
    """获取弹窗元素的高度（用于过滤小元素）."""
    try:
      box = await modal.bounding_box()
      return box["height"] if box else 0
    except Exception:
      return 0

  async def _has_nearby_plan_text(self, btn, plan_keys: list[str]) -> bool:
    """
    检查按钮的父元素（最多向上5层）是否包含套餐关键词.
    通过 JS eval 在浏览器中执行，避免 Playwright ElementHandle 的局限.
    """
    try:
      # 在浏览器上下文中执行 JS：
      # 从当前元素向上遍历5层父节点，检查 textContent 是否包含关键词
      result = await btn.evaluate(
          """(el, keys) => {
              let node = el;
              for (let i = 0; i < 5; i++) {
                  node = node.parentElement;  // 向上找父元素
                  if (!node) break;
                  const text = node.textContent || '';
                  if (keys.some(k => text.includes(k))) return true;
              }
              return false;
          }""",
          plan_keys,  # 把 Python 列表传给 JS
      )
      return bool(result)
    except Exception:
      return False

  async def has_any_purchase_button(self) -> bool:
    """
    检查页面上是否存在任何可点击的购买按钮（不含"售罄"的）.
    用于 auto_snipe_on_ready() 判断页面是否已恢复正常.
    """
    buttons = await self._page.locator("button").all()
    for btn in buttons:
      try:
        if not await self._is_visible(btn):
          continue  # 不可见跳过
        text = await btn.text_content() or ""
        text = text.strip()
        if any(kw in text for kw in SOLD_OUT_KEYWORDS):
          continue  # 售罄按钮跳过
        if any(kw in text for kw in BUY_KEYWORDS):
          return True  # 找到了可用的购买按钮
      except Exception:
        continue
    return False
