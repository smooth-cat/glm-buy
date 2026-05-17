"""
人类化鼠标移动和点击模拟 — 使用 Playwright 的底层鼠标 API.

核心原则：所有点击都必须经过真实的鼠标移动(mouse.move) + 鼠标点击(mouse.click)，
绝不使用 element.click() 或 dispatchEvent().

对应 JS 版中 tryClickPurchaseButton() 和 tryClickConfirmButton() 的点击部分，
但 JS 版直接调 element.click()，Python 版改为真实鼠标移动.
"""

import asyncio
import math
import random

from loguru import logger


def _bezier_curve(
    start: tuple[float, float],
    end: tuple[float, float],
    steps: int = 10,
) -> list[tuple[float, float]]:
  """
  生成从 start 到 end 的二次贝塞尔曲线路径.

  贝塞尔曲线让鼠标移动看起来像人类手臂运动（有自然的弧度），
  而不是机械的直线移动.

  参数:
    start: 起点坐标 (x, y)
    end:   终点坐标 (x, y)
    steps: 路径上的采样点数
  返回: 路径点列表 [(x, y), ...]
  """
  # 计算起点和终点的中点
  mid_x = (start[0] + end[0]) / 2
  mid_y = (start[1] + end[1]) / 2

  # 在垂直方向上随机偏移控制点，制造弧形轨迹
  dx = end[0] - start[0]
  dy = end[1] - start[1]
  # 偏移量：20-80像素，方向随机（左弧或右弧）
  offset = random.randint(20, 80) * random.choice([-1, 1])

  # 计算垂直方向的单位向量，将控制点推向侧方
  length = math.hypot(dx, dy) or 1  # 欧几里得距离，防止除以零
  ctrl_x = mid_x + (-dy / length) * offset + random.randint(-30, 30)
  ctrl_y = mid_y + (dx / length) * offset + random.randint(-30, 30)

  # 二次贝塞尔公式: B(t) = (1-t)²P₀ + 2(1-t)tP₁ + t²P₂
  points: list[tuple[float, float]] = []
  for i in range(steps + 1):
    t = i / steps  # t 从 0 到 1
    x = (1 - t) ** 2 * start[0] + 2 * (1 - t) * t * ctrl_x + t**2 * end[0]
    y = (1 - t) ** 2 * start[1] + 2 * (1 - t) * t * ctrl_y + t**2 * end[1]
    points.append((x, y))
  return points


class Mouse:
  """
  模拟人类鼠标交互.

  每次 move_to() 都会走贝塞尔曲线而非直线，
  每次 click() 都会在目标位置加随机偏移，
  所有操作间有随机的微延迟.
  """

  def __init__(self, page) -> None:
    """初始化，page 是 Playwright 的 Page 对象."""
    self._page = page
    # 鼠标初始位置：在屏幕上随机一个点（模拟真实用户开始使用）
    self._current_pos: tuple[float, float] = (
        random.randint(100, 500), random.randint(100, 400))

  async def move_to(
      self,
      x: float,
      y: float,
      steps: int | None = None,
  ) -> None:
    """
    从当前鼠标位置移动到目标坐标 (x, y).
    使用贝塞尔曲线路径，步数随机.
    """
    # 如果未指定步数，随机 5-15 步（步数越多，运动越平滑、越慢）
    if steps is None:
      steps = random.randint(5, 15)

    # 目标位置加入 ±3px 随机抖动（人类不会每次都点同一个像素）
    target_x = x + random.randint(-3, 3)
    target_y = y + random.randint(-3, 3)

    # 生成贝塞尔路径
    path = _bezier_curve(self._current_pos, (target_x, target_y), steps)

    # 逐帧移动鼠标
    for px, py in path:
      await self._page.mouse.move(px, py)  # Playwright 底层鼠标移动 API
      # 帧间微延迟（2-8ms），模拟人类手部运动的不连续性
      await asyncio.sleep(random.uniform(0.002, 0.008))

    # 记住当前位置，供下次 move_to 使用
    self._current_pos = (target_x, target_y)

  async def click(
      self,
      x: float,
      y: float,
      delay_ms: int | None = None,
  ) -> None:
    """
    移动到 (x, y) 并点击.
    点击前有随机微延迟（模拟人类的反应时间）.
    """
    # 先移动到目标位置
    await self.move_to(x, y)

    # 随机延迟 50-200ms（模拟看到按钮后的反应时间）
    if delay_ms is None:
      delay_ms = random.randint(50, 110)
    await asyncio.sleep(delay_ms / 1000)

    # 执行点击（Playwright 底层鼠标点击 API）
    await self._page.mouse.click(x, y)
    logger.debug(f"鼠标点击 ({x:.0f}, {y:.0f})")

  async def click_element_center(self, element, delay_ms: int | None = None) -> bool:
    """
    获取元素的 bounding box，计算中心点（带随机偏移），移动并点击.
    返回 False 表示元素不可见/不可点击.

    这个方法比 find → boundingBox → click 三步合一的便捷方法.
    """
    try:
      # 获取元素在页面上的位置和尺寸
      box = await element.bounding_box()
      if box is None:
        return False  # 元素不可见或不在视口内

      # 在元素内部随机偏移点击位置（避免总点正中心，更像人类）
      offset_x = random.randint(-int(box["width"] * 0.2),
                                int(box["width"] * 0.2))
      offset_y = random.randint(-int(box["height"] * 0.2),
                                int(box["height"] * 0.2))
      # 最终点击坐标 = 中心 + 随机偏移
      cx = box["x"] + box["width"] / 2 + offset_x
      cy = box["y"] + box["height"] / 2 + offset_y

      await self.click(cx, cy, delay_ms)
      return True
    except Exception:
      return False

  async def random_idle(self, min_ms: int = 50, max_ms: int = 150) -> None:
    """
    随机空闲一段时间.
    用于在两次操作之间插入人类停顿.
    """
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)
