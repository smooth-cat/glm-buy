"""
商品 ID 管理模块 — 对应 JS 版 product 相关逻辑.

核心功能:
1. 通过价格识别套餐类型（Lite=49元, Pro=149元, Max=469元）
2. 通过 campaign 名称识别计费周期（包月/包季/包年）
3. 从 batch-preview 和 productinfo API 响应中提取 productId
4. 多级 fallback 获取 productId（内存 → 精确匹配 → 候选表 → 本地缓存）
5. 本地 JSON 文件缓存（替代 JS 版的 localStorage）
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

# ==================== 常量映射表 ====================

# 价格 → 套餐名：API 返回的 monthlyOriginalAmount 精确对应
PLAN_PRICE_MAP = {"lite": 49, "pro": 149, "max": 469}

# 价格范围兜底表：当原价被打折时，用范围匹配
# 三档之间差距足够大（30-80, 100-200, 350-550），不会重叠
PLAN_PRICE_RANGES = {
    "lite": (30, 80),
    "pro": (100, 200),
    "max": (350, 550),
}

# campaignName 关键词 → 计费周期：
# 检测 API 返回的 campaignDiscountDetails[].campaignName
PERIOD_PATTERNS = {
    "quarterly": ["包季", "季度", "季卡", "3个月", "三个月"],
    "yearly": ["包年", "年度", "年卡", "12个月", "一年"],
}
# 未匹配到任何模式的默认为 monthly（包月）

# productinfo API 候选表的促销 key 黑名单（与 plan 类型无关的 key）
PROMO_KEYS = ["newUserPurchase", "CCFold", "newUser", "fold", "promo", "trial"]

# 本地缓存文件路径和有效期（12小时）
CACHE_FILE = Path("glm_sniper_pid.json")
CACHE_TTL_MS = 12 * 3600 * 1000  # 12 hours in milliseconds


@dataclass
class PlanInfo:
  """
  从 API 数据中识别出的套餐信息.
  """
  plan: str       # 'lite' | 'pro' | 'max'
  period: str     # 'monthly' | 'quarterly' | 'yearly'
  product_id: str # 形如 "product-xxxxxxxx"


class ProductManager:
  """
  商品 ID 管理器.
  负责 productId 的发现、缓存、匹配和检索.
  """

  def __init__(self) -> None:
    # 已捕获的目标商品 productId（单一值）
    self._captured_product_id: str | None = None

    # 所有已识别的 productId，key 格式: "lite_quarterly" → "product-xxx"
    self._all_product_ids: dict[str, str] = {}

    # productinfo API 返回的候选 productId 表（平铺的 key→pid）
    self._product_info_candidates: dict[str, str] = {}

  # ==================== Plan/Period 识别 ====================

  @staticmethod
  def get_period_from_item(item: dict) -> str:
    """
    从 API 商品数据中静默识别计费周期（不打日志，用于热路径）.
    与 JS 版 getPeriodFromItem() 完全对应.
    """
    campaigns = item.get("campaignDiscountDetails", []) or []
    for c in campaigns:
      cn = c.get("campaignName", "")
      for period, patterns in PERIOD_PATTERNS.items():
        if any(kw in cn for kw in patterns):
          return period
    return "monthly"

  def identify_plan_from_product(self, item: dict) -> PlanInfo | None:
    """
    从 batch-preview 返回的商品条目中识别套餐名和计费周期.
    识别逻辑：
      1. 价格精确匹配 → 确定 plan
      2. 价格范围兜底 → 确定 plan
      3. campaignName 关键词 → 确定 period
    返回: PlanInfo 或 None（无法识别时）
    """
    # 第一步：获取价格
    price = item.get("monthlyOriginalAmount")
    if price is None:
      return None

    # 第二步：价格 → plan（先精确匹配，再范围兜底）
    plan: str | None = None
    for name, p in PLAN_PRICE_MAP.items():
      if price == p:
        plan = name
        break

    if plan is None:
      # 精确匹配失败，尝试范围兜底（原价可能被折扣）
      for name, (lo, hi) in PLAN_PRICE_RANGES.items():
        if lo <= price <= hi:
          plan = name
          logger.info(
              f"[识别] 价格 {price} 精确匹配失败，范围兜底 → {plan}"
          )
          break

    if plan is None:
      # 价格既不在精确表也不在范围表中，无法识别
      logger.warning(
          f"[识别失败] 未知价格: {price}, productId={item.get('productId')}"
      )
      return None

    # 第三步：campaignName → period
    period = "monthly"       # 默认为包月
    matched_campaign = ""    # 匹配到的 campaignName（仅日志用）
    campaigns = item.get("campaignDiscountDetails", []) or []
    for c in campaigns:
      cn = c.get("campaignName", "")
      for p, patterns in PERIOD_PATTERNS.items():
        if any(kw in cn for kw in patterns):
          period = p
          matched_campaign = cn
          break
      if period != "monthly":  # 找到非默认周期就停止
        break

    pid = item.get("productId", "")
    logger.info(
        f"[识别] 价格={price} → {plan} | "
        f'campaignName="{matched_campaign}" → {period} | productId={pid}'
    )
    return PlanInfo(plan=plan, period=period, product_id=pid)

  # ==================== Product ID 捕获 ====================

  def capture_product_id_from_data(
      self, obj: Any, current_plan: str, current_period: str
  ) -> None:
    """
    递归检查 API 响应数据，提取 productId.
    支持三种数据结构：
      1. batch-preview 响应: { productList: [...] }
      2. productinfo 响应: { key1: "product-xxx", key2: "product-yyy", ... }
      3. 嵌套的 data 字段（递归查找）
    """
    # 非字典或为空，跳过
    if not obj or not isinstance(obj, dict):
      return

    # ---- 结构1: batch-preview, productList 数组 ----
    if "productList" in obj and isinstance(obj["productList"], list):
      # 诊断：列出未知价格（帮助发现平台调价）
      unknown_prices = [
          item.get("monthlyOriginalAmount")
          for item in obj["productList"]
          if isinstance(item, dict)
          and item.get("monthlyOriginalAmount") not in PLAN_PRICE_MAP.values()
      ]
      if unknown_prices:
        logger.debug(
            f"[诊断] productList 中存在未知价格: {unknown_prices}"
        )

      # 遍历 productList 中的每一项
      for item in obj["productList"]:
        if not isinstance(item, dict) or not item.get("productId"):
          continue
        # 识别 plan 和 period
        info = self.identify_plan_from_product(item)
        if info is None:
          continue
        # 存储到 _all_product_ids（以 "plan_period" 为 key）
        key = f"{info.plan}_{info.period}"
        self._all_product_ids[key] = info.product_id

        # 如果匹配当前目标套餐，则设为主要 productId 并缓存
        if info.plan == current_plan and info.period == current_period:
          self._captured_product_id = info.product_id
          self._save_to_cache(info.product_id, current_plan, current_period)

      if self._all_product_ids and not self._captured_product_id:
        logger.info(
            f"[捕获] 找到{len(self._all_product_ids)}个产品，"
            f"但未匹配目标套餐 {current_plan}/{current_period}"
        )
        logger.debug(f"[诊断] 已捕获套餐: {list(self._all_product_ids)}")
      return

    # ---- 结构2: productinfo API 平铺 key→productId ----
    pid_pattern = "product-"  # productId 前缀
    flat_entries = [
        (k, v)
        for k, v in obj.items()
        if isinstance(v, str) and v.startswith(pid_pattern)
    ]
    if len(flat_entries) >= 2:  # 至少2个才认为是 productinfo 结构
      new_candidates = False
      for k, pid in flat_entries:
        if k not in self._product_info_candidates:
          self._product_info_candidates[k] = pid
          new_candidates = True

      if new_candidates:
        logger.debug(
            f"[productinfo] 捕获候选表: "
            f'{", ".join(f"{k}→{v}" for k, v in flat_entries)}'
        )
        # 如果还没有 productId，尝试从候选表中匹配
        if not self._captured_product_id:
          pid = self._get_product_id_from_candidates(
              current_plan, current_period
          )
          if pid:
            self._captured_product_id = pid
            logger.info(f"[productinfo] 已设定 productId={pid}")
            self._save_to_cache(pid, current_plan, current_period)
      return

    # ---- 结构3: 递归查找嵌套的 data 字段 ----
    data = obj.get("data")
    if isinstance(data, dict):
      self.capture_product_id_from_data(data, current_plan, current_period)
    elif isinstance(data, list):
      for item in data:
        if isinstance(item, dict):
          self.capture_product_id_from_data(
              item, current_plan, current_period
          )

  # ==================== Product ID 检索 ====================

  def get_product_id(self, current_plan: str, current_period: str) -> str | None:
    """
    获取 productId — 多级 fallback 策略:
      1. 内存中已捕获的 productId（直接返回）
      2. 精确匹配："plan_period" key
      3. 同套餐匹配：相同 plan，不同 period
      4. productinfo 候选表智能匹配
      5. 本地 JSON 文件缓存（12小时 TTL）
    """
    # 1. 已捕获，直接返回
    if self._captured_product_id:
      return self._captured_product_id

    # 2. 精确匹配 "lite_quarterly" → productId
    exact_key = f"{current_plan}_{current_period}"
    if exact_key in self._all_product_ids:
      self._captured_product_id = self._all_product_ids[exact_key]
      logger.info(f"[回退] 精确匹配 productId={self._captured_product_id}")
      return self._captured_product_id

    # 3. 同套餐不同周期匹配（如目标是 lite_quarterly，但有 lite_monthly）
    for key, pid in self._all_product_ids.items():
      if key.startswith(f"{current_plan}_"):
        self._captured_product_id = pid
        logger.info(f"[回退] 同套餐匹配 productId={pid} ({key})")
        return pid

    # 如有其他产品但无法匹配，发出警告
    if self._all_product_ids:
      logger.warning(
          f"[警告] 已捕获产品但无法匹配 {current_plan}/{current_period}"
      )

    # 4. productinfo 候选表智能匹配
    candidate = self._get_product_id_from_candidates(
        current_plan, current_period)
    if candidate:
      self._captured_product_id = candidate
      logger.info(f"[回退] 从 productinfo 候选表获取 productId={candidate}")
      return candidate

    # 5. 本地缓存兜底（12小时有效期）
    cached = self._load_from_cache(current_plan, current_period)
    if cached:
      self._captured_product_id = cached
      logger.info(f"[回退] 从本地缓存恢复 productId={cached}")
      return cached

    return None

  def _get_product_id_from_candidates(
      self, plan: str, period: str
  ) -> str | None:
    """
    从 productinfo 候选表中智能匹配 productId.
    匹配策略（优先级从高到低）:
      1. key 包含 plan 或 period 关键词（且非促销 key）
      2. 过滤促销 key 后只剩一个，直接使用
      3. 过滤促销 key 后有多个，使用第一个
      4. 全是促销 key，兜底使用第一个
    """
    if not self._product_info_candidates:
      return None

    entries = list(self._product_info_candidates.items())

    # 策略1: 关键词精确匹配
    for k, pid in entries:
      kl = k.lower()
      if (plan in kl or period in kl) and not any(
          p in kl for p in PROMO_KEYS
      ):
        logger.info(f"[候选] key={k!r} 命中关键词 → productId={pid}")
        return pid

    # 策略2: 过滤促销 key 后唯一
    filtered = [
        (k, v)
        for k, v in entries
        if not any(p in k.lower() for p in PROMO_KEYS)
    ]
    if len(filtered) == 1:
      logger.info(f"[候选] 唯一非促销候选: {filtered[0][0]}→{filtered[0][1]}")
      return filtered[0][1]

    # 策略3: 多个非促销候选，取第一个
    if len(filtered) > 1:
      logger.info(
          f"[候选] 多个非促销候选: "
          f'[{", ".join(f"{k}→{v}" for k, v in filtered)}]，'
          f"使用第一个"
      )
      return filtered[0][1]

    # 策略4: 全是促销 key，兜底
    logger.info(f"[候选] 仅有促销类候选，兜底使用第一个: {entries[0][0]}")
    return entries[0][1]

  # ==================== 本地缓存（替代 JS 版 localStorage）====================

  def _save_to_cache(self, pid: str, plan: str, period: str) -> None:
    """
    将 productId 持久化到本地 JSON 文件.
    格式: {"id": "...", "plan": "lite", "period": "quarterly", "ts": 时间戳ms}
    """
    try:
      CACHE_FILE.write_text(
          json.dumps(
              {
                  "id": pid,
                  "plan": plan,
                  "period": period,
                  "ts": int(time.time() * 1000),  # JavaScript 风格毫秒时间戳
              },
              ensure_ascii=False,  # 不转义中文
          )
      )
    except Exception as e:
      logger.debug(f"缓存写入失败: {e}")

  def _load_from_cache(self, plan: str, period: str) -> str | None:
    """
    从本地缓存文件读取 productId.
    只有当 plan、period 匹配且未超过12小时才有效.
    """
    try:
      if not CACHE_FILE.exists():
        return None
      data = json.loads(CACHE_FILE.read_text())
      # 验证缓存有效性：plan+period 匹配，且未过期
      if (
          data.get("id")
          and data.get("plan") == plan
          and data.get("period") == period
          and int(time.time() * 1000) - data.get("ts", 0) < CACHE_TTL_MS
      ):
        return data["id"]
    except Exception:
      pass  # 缓存文件损坏或格式错误，静默跳过
    return None

  # ==================== 属性访问器 ====================

  @property
  def captured_product_id(self) -> str | None:
    """获取当前捕获的 productId."""
    return self._captured_product_id

  @captured_product_id.setter
  def captured_product_id(self, value: str | None) -> None:
    """
    设置 productId（用于外部重置，比如切换到候补套餐时清空旧的 productId）.
    """
    self._captured_product_id = value
