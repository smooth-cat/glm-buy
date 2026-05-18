"""
验证码自动通过 — 独立演示脚本.

DOM 选择器集中在 config.py 的 CAPTCHA_SELECTORS 中，兼容不同平台的验证码.
运行方式: uv run python -m glm_buy.verify.demo
"""

from __future__ import annotations

import asyncio
import re
import tkinter as tk
from pathlib import Path

import cv2
import numpy as np
from loguru import logger
from playwright.async_api import async_playwright

from glm_buy.config import CAPTCHA_SELECTORS, config
from glm_buy.logger import setup_logging
from glm_buy.mouse import Mouse
from glm_buy.verify.solver import CaptchaSolver

S = CAPTCHA_SELECTORS  # 简写


def get_screen_size() -> tuple[int, int]:
  root = tk.Tk()
  root.withdraw()
  w = root.winfo_screenwidth()
  h = root.winfo_screenheight()
  root.destroy()
  return w, h


async def main() -> None:
  setup_logging()
  logger.info("=" * 50)
  logger.info("验证码自动通过演示 — 启动")
  logger.info("=" * 50)

  solver = CaptchaSolver()

  screen_w, screen_h = get_screen_size()
  logger.info(f"屏幕分辨率: {screen_w}x{screen_h}")
  logger.info("启动浏览器...")
  async with async_playwright() as pw:
    browser = await pw.chromium.launch(
        headless=False,
        args=["--no-first-run", "--no-default-browser-check"],
    )
    context = await browser.new_context(
        viewport={"width": screen_w, "height": screen_h - 100},
        locale="zh-CN",
    )
    page = await context.new_page()
    mouse = Mouse(page)

    # 打开腾讯云验证码产品页
    logger.info(f"打开页面: {S['demo_page_url']}")
    await page.goto(S["demo_page_url"], wait_until="domcontentloaded")
    await asyncio.sleep(3)

    # 点击"文字点选验证"标签
    logger.info("查找文字点选验证标签...")
    try:
      tab_text = S["text_verify_tab"]
      text_tab = page.locator(f"text={tab_text}").first
      if await text_tab.count() == 0:
        text_tab = page.locator(f"span:has-text('{tab_text}')").first
      await text_tab.click()
      logger.info("已点击文字点选验证标签")
      await asyncio.sleep(1)
    except Exception as e:
      logger.error(f"点击标签失败: {e}")
      await browser.close()
      return

    # 点击"立即体验"按钮
    box_sel = S["box_container"]
    logger.info(f"在 {box_sel} 内查找立即体验按钮...")
    try:
      box_container = page.locator(box_sel)
      if await box_container.count() == 0:
        logger.error(f"未找到 {box_sel} 容器")
        await browser.close()
        return

      btn_text = S["experience_btn"]
      demo_btn = box_container.locator(f"button:has-text('{btn_text}')").first
      if await demo_btn.count() == 0:
        demo_btn = box_container.locator(f"a:has-text('{btn_text}')").first

      box = await demo_btn.bounding_box()
      if box:
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        await mouse.click(cx, cy)
        logger.info("已点击立即体验按钮")
      else:
        logger.error("未找到立即体验按钮的位置")
        await browser.close()
        return
    except Exception as e:
      logger.error(f"点击按钮失败: {e}")
      await browser.close()
      return

    # 等待验证码弹窗出现 → 识别 + 点击 + 重试
    captcha_solved = await solve_captcha(page, mouse, solver)
    if not captcha_solved:
      logger.warning("验证码未能自动通过")
      await browser.close()
      return

    logger.info("等待验证结果...")
    await asyncio.sleep(5)

    try:
      body_text = await page.text_content("body") or ""
      if any(kw in body_text for kw in ["验证成功", "验证通过", "success", "通过"]):
        logger.info("验证通过！")
      else:
        logger.info("未检测到明确的成功提示，请观察浏览器窗口")
    except Exception:
      pass

    logger.info("保持浏览器打开 5 秒...")
    await asyncio.sleep(5)
    # await browser.close()
    logger.info("=" * 50)
    logger.info("演示结束")
    logger.info("=" * 50)


# ==================== 公开接口 ====================

async def solve_captcha(page, mouse, solver, max_attempts: int = 3) -> bool:
  """
  等待验证码弹窗出现 → 识别 + 点击 + 重试.
  返回 True 表示验证码已通过.

  可在主抢购流程中复用:
      from glm_buy.verify.demo import solve_captcha
      ok = await solve_captcha(page, mouse, solver)
  """
  tcaptcha_sel = S["opera"]
  logger.info(f"等待验证码弹窗出现（监控 {tcaptcha_sel} 元素）...")
  tcaptcha = None
  deadline = asyncio.get_event_loop().time() + 15
  while not tcaptcha:
    tcaptcha, tcaptcha_frame = await _find_in_frames(page, tcaptcha_sel)
    if not tcaptcha:
      if asyncio.get_event_loop().time() >= deadline:
        logger.error(f"验证码弹窗未在 15 秒内出现: 未找到 {tcaptcha_sel}")
        return False
      await asyncio.sleep(0.5)
  # 等到元素真正可见（iframe 中可能 DOM 已存在但未渲染）
  try:
    await tcaptcha.wait_for(state="visible", timeout=10000)
  except Exception:
    pass
  logger.info("验证码弹窗已出现")
  await asyncio.sleep(config.captcha_appear_delay_ms / 1000)

  for attempt in range(1, max_attempts + 1):
    logger.info(f"--- 第 {attempt}/{max_attempts} 次识别尝试 ---")

    captcha_screenshot, offset_x, offset_y = await _capture_captcha(page)
    prompt_text = await _extract_prompt(page)

    screenshot_path = Path("logs") / f"captcha_attempt{attempt}.png"
    screenshot_path.parent.mkdir(exist_ok=True)
    screenshot_path.write_bytes(captcha_screenshot)
    logger.info(f"截图已保存: {screenshot_path}")
    logger.info(f"提示语: {prompt_text or '(未检测到)'}")

    clicked = await _recognize_and_click(
        solver, mouse, captcha_screenshot, prompt_text, offset_x, offset_y
    )
    need_refresh = False
    if clicked:
      await _click_confirm_button(page, mouse)
      await asyncio.sleep(config.captcha_verify_delay_ms / 1000)
      # 检查是否有可见的验证错误元素（而非整页文本，避免读到已隐藏的旧错误）
      visible_error = False
      for f in [page] + [f for f in page.frames if f != page]:
        try:
          for kw in ("验证错误", "请重试"):
            for el in await f.locator(f"text={kw}").all():
              if await el.is_visible():
                visible_error = True
                break
            if visible_error:
              break
        except Exception:
          pass
        if visible_error:
          break
      if visible_error:
        logger.info("验证错误，需要刷新重试")
        need_refresh = True
      else:
        return True
    else:
      logger.info("OCR 未匹配到目标文字，需要刷新重试")
      need_refresh = True

    if need_refresh and attempt < max_attempts:
      logger.info("点击刷新按钮...")
      try:
        refresh_btn, _ = await _find_in_frames(page, S["action_refresh"])
        if refresh_btn:
          box = await refresh_btn.bounding_box()
          if box:
            await mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            logger.info("已点击验证码刷新按钮")
            await _wait_captcha_loading(page)
            await asyncio.sleep(config.captcha_refresh_delay_ms / 1000)
          else:
            logger.warning("刷新按钮不可见")
        else:
          logger.warning(f"未找到 {S['action_refresh']} 按钮")
      except Exception as e:
        logger.error(f"刷新验证码失败: {e}")

  logger.warning(f"全部 {max_attempts} 次识别尝试均失败")
  return False


# ==================== 内部辅助函数 ====================

async def _find_in_frames(page, selector: str):
  """主页面 → 所有 iframe frame 依次搜索，返回 (locator, frame) 或 (None, None)."""
  frames = [page] + [f for f in page.frames if f != page]
  for frame in frames:
    try:
      el = frame.locator(selector).first
      if await el.count() > 0:
        return el, frame
    except Exception:
      continue
  return None, None


async def _capture_captcha(page):
  """截取验证码图片，返回 (bytes, offset_x, offset_y)."""
  offset_x, offset_y = 0, 0

  bg_img, frame = await _find_in_frames(page, S["opera"])
  if bg_img:
    box = await bg_img.bounding_box()
    if box:
      offset_x, offset_y = box["x"], box["y"]
      # iframe 内元素：补上 iframe 在父页面中的偏移
      if frame and frame is not page:
        try:
          iframe_box = await frame.frame_element().bounding_box()
          if iframe_box:
            offset_x += iframe_box["x"]
            offset_y += iframe_box["y"]
            logger.info(
                f"iframe 偏移: ({iframe_box['x']:.0f},{iframe_box['y']:.0f})"
            )
        except Exception:
          pass
      screenshot = await bg_img.screenshot()
      logger.info(f"截取 {S['opera']} (offset=({offset_x:.0f},{offset_y:.0f}))")
      return screenshot, offset_x, offset_y

  # captcha_box, _ = await _find_in_frames(page, S["container"])
  # if captcha_box:
  #   box = await captcha_box.bounding_box()
  #   if box:
  #     offset_x, offset_y = box["x"], box["y"]
  #   screenshot = await captcha_box.screenshot()
  #   logger.info(f"截取 {S['container']} (offset=({offset_x:.0f},{offset_y:.0f}))")
  #   return screenshot, offset_x, offset_y

  logger.warning("未能截取验证码区域，使用全屏截图")
  screenshot = await page.screenshot()
  return screenshot, offset_x, offset_y


async def _extract_prompt(page) -> str:
  """从主页或 iframe 提取提示语."""
  try:
    for frame in [page] + [f for f in page.frames if f != page]:
      try:
        body = await frame.text_content("body") or ""
        match = re.search(
            r'(?:请依次点击|按顺序点击)[：:\s]*([一-鿿](?:\s*[一-鿿]){1,5})',
            body,
        )
        if match:
          logger.info(f"提取到提示语: {match.group(0)}")
          return match.group(0)
      except Exception:
        continue
    logger.warning("未找到提示语")
  except Exception as e:
    logger.debug(f"提取提示语失败: {e}")
  return ""


async def _wait_captcha_loading(page) -> None:
  """等待验证码加载完成：若 loading 可见则等它消失，否则忽略（加载太快已结束）."""
  try:
    _, frame = await _find_in_frames(page, S["opera"])
    if not frame:
      await asyncio.sleep(0.5)
      return
    loading = frame.locator(S["opera_loading"]).first
    # 如果 loading 当前可见，等它消失；不可见说明已经加载完（或太快跳过了）
    if await loading.count() > 0 and await loading.is_visible():
      logger.info("验证码加载中...")
      await loading.wait_for(state="hidden", timeout=8000)
      logger.info("验证码加载完成")
    # 否则已经加载好了，不需要等
  except Exception as e:
    logger.debug(f"等待加载异常（回退 0.5s）: {e}")
    await asyncio.sleep(0.5)


async def _click_confirm_button(page, mouse) -> None:
  """在所有 frame 中搜索确认按钮并点击."""
  try:
    await asyncio.sleep(config.captcha_confirm_delay_ms / 1000)
    confirm = S["confirm_btn_text"]
    for frame in [page] + [f for f in page.frames if f != page]:
      try:
        for el in await frame.locator(
            f"div:has-text('{confirm}'), button:has-text('{confirm}'), "
            f"span:has-text('{confirm}'), a:has-text('{confirm}')"
        ).all():
          if not await el.is_visible():
            continue
          box = await el.bounding_box()
          if box and box["width"] < 200 and box["height"] < 100:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            await mouse.click(cx, cy)
            logger.info(f"已点击确认按钮")
            return
      except Exception:
        continue

    logger.info("未找到确认按钮，各 frame 可见文字:")
    for i, frame in enumerate([page] + [f for f in page.frames if f != page]):
      try:
        txt = (await frame.text_content("body") or "").replace("\n", " ")[:200]
        logger.info(f"  frame[{i}]: {txt}")
      except Exception:
        pass
  except Exception as e:
    logger.info(f"点击确认按钮失败: {e}")


async def _recognize_and_click(
    solver, mouse, captcha_screenshot: bytes,
    prompt_text: str, offset_x: float, offset_y: float,
) -> bool:
  """识别验证码并点击，返回 True 表示成功."""
  if not prompt_text:
    logger.warning("无提示语，检测全部文字按空间顺序点击...")
    try:
      nparr = np.frombuffer(captcha_screenshot, np.uint8)
      img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
      if img is None:
        return False
      items = solver._predict(img)
      if items:
        items.sort(key=lambda p: (p[7], p[6]))
        logger.info(f"检测到 {len(items)} 个文字，按空间顺序点击")
        for item in items:
          _, _, _, _, text, conf, cx, cy = item
          page_cx, page_cy = cx + offset_x, cy + offset_y
          logger.info(f"  '{text}'({conf:.0%}) → 页面({page_cx:.0f},{page_cy:.0f})")
          await mouse.click(page_cx, page_cy)
          await asyncio.sleep(config.captcha_click_interval_ms / 1000)
        return True
      return False
    except Exception as e:
      logger.error(f"识别失败: {e}")
      return False

  clicks = solver.solve(captcha_screenshot, prompt_text)
  if clicks:
    logger.info(f"识别成功！共 {len(clicks)} 个点击点")
    for cx, cy in clicks:
      page_cx, page_cy = cx + offset_x, cy + offset_y
      logger.info(f"  图内({cx},{cy}) → 页面({page_cx:.0f},{page_cy:.0f})")
      await mouse.click(page_cx, page_cy)
      await asyncio.sleep(config.captcha_click_interval_ms / 1000)
    return True
  return False


if __name__ == "__main__":
  asyncio.run(main())
