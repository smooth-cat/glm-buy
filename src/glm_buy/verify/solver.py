"""
验证码识别器 — PaddleOCR 检测 + 识别文字点选验证码.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from loguru import logger
from paddleocr import PaddleOCR


class CaptchaSolver:
  """文字点选验证码识别器."""

  def __init__(self) -> None:
    logger.info("正在初始化 PaddleOCR...")
    self._engine = PaddleOCR(
        lang="ch",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        text_det_thresh=0.3,
        text_det_box_thresh=0.4,
        text_det_unclip_ratio=0.6,  # 激进收缩，防止相邻字合并
        text_rec_score_thresh=0.3,
    )
    logger.info("验证码识别器初始化完成")

  def solve(
      self, image_bytes: bytes, prompt_text: str
  ) -> list[tuple[int, int]]:
    """识别验证码，返回按提示语顺序的点击坐标."""
    target_chars = self._parse_prompt(prompt_text)
    if not target_chars:
      return []

    try:
      img_array = self._bytes_to_numpy(image_bytes)
    except Exception:
      return []

    self._save_debug_image(image_bytes, "captcha_original")
    logger.info(f"目标文字: {target_chars}")

    # 原图 → PaddleOCR 检测+识别
    raw = self._predict(img_array)
    raw_texts = [t[4] for t in raw]
    logger.info(f"OCR原始:  {raw_texts}")

    # 拆字：多字框均分宽度
    raw = self._split_merged(raw)
    if [t[4] for t in raw] != raw_texts:
      logger.info(f"拆分后:  {[t[4] for t in raw]}")

    # 建立映射
    word_positions = self._build_map(raw)
    logger.info(f"过滤后:  {list(word_positions.keys())}")

    # 匹配目标
    result = self._match_targets(word_positions, target_chars)
    if result:
      return result

    # 原图失败 → 预处理重试
    logger.info("原图不满足目标，尝试预处理图...")
    preprocessed = self._preprocess(image_bytes)
    self._save_debug_image(preprocessed, "captcha_preprocessed")
    preprocessed_array = self._bytes_to_numpy(preprocessed)
    raw2 = self._predict(preprocessed_array)
    raw2 = self._split_merged(raw2)
    word_positions2 = self._build_map(raw2)
    return self._match_targets(word_positions2, target_chars) or []

  # ==================== PaddleOCR 检测 ====================

  def _predict(self, img_array: np.ndarray) -> list[tuple[int, int, int, int, str, float, int, int]]:
    """PaddleOCR 全图检测+识别，返回 [(x1,y1,x2,y2,text,conf,cx,cy), ...]."""
    page = self._engine.predict(img_array)
    if not page:
      return []
    data = page[0]
    items: list[tuple[int, int, int, int, str, float, int, int]] = []
    for poly, text, score in zip(data["dt_polys"], data["rec_texts"], data["rec_scores"]):
      xs, ys = poly[:, 0].astype(int), poly[:, 1].astype(int)
      x1, y1, x2, y2 = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
      cx, cy = int(xs.mean()), int(ys.mean())
      items.append((x1, y1, x2, y2, str(text), float(score), cx, cy))
    return items

  # ==================== 拆字（均分） ====================

  @staticmethod
  def _split_merged(
      items: list[tuple[int, int, int, int, str, float, int, int]],
  ) -> list[tuple[int, int, int, int, str, float, int, int]]:
    """
    多字检测框按宽高比自动判断拆分方向.
    - 宽高比 > 2: 横向均分（如"AB"→"A""B"）
    - 高宽比 > 2: 纵向均分（如上下两字合并）
    - 其他: 按短边均分
    """
    result: list[tuple[int, int, int, int, str, float, int, int]] = []
    for x1, y1, x2, y2, text, conf, cx, cy in items:
      n = len(text)
      if n <= 1:
        result.append((x1, y1, x2, y2, text, conf, cx, cy))
        continue
      bw, bh = x2 - x1, y2 - y1
      if bw / max(bh, 1) >= 2:
        # 横向均分
        cw = bw / n
        for i in range(n):
          sx1 = int(x1 + i * cw)
          sx2 = int(x1 + (i + 1) * cw)
          result.append((sx1, y1, sx2, y2, text[i], conf, (sx1 + sx2) // 2, cy))
      elif bh / max(bw, 1) >= 2:
        # 纵向均分
        ch = bh / n
        for i in range(n):
          sy1 = int(y1 + i * ch)
          sy2 = int(y1 + (i + 1) * ch)
          result.append((x1, sy1, x2, sy2, text[i], conf, cx, (sy1 + sy2) // 2))
      else:
        # 几乎方形 → 保留原结果
        result.append((x1, y1, x2, y2, text, conf, cx, cy))
    return result

  # ==================== 映射 & 匹配 ====================

  @staticmethod
  def _build_map(
      items: list[tuple[int, int, int, int, str, float, int, int]],
  ) -> dict[str, list[tuple[int, int]]]:
    """过滤并建立 {文字: [坐标, ...]}."""
    m: dict[str, list[tuple[int, int]]] = {}
    for _, _, _, _, text, conf, cx, cy in items:
      if conf < 0.7 or len(text) > 3 or "AI" in text or "生成" in text:
        continue
      if text not in m:
        m[text] = []
      m[text].append((cx, cy))
    return m

  @staticmethod
  def _match_targets(
      word_positions: dict[str, list[tuple[int, int]]],
      target_chars: list[str],
  ) -> list[tuple[int, int]] | None:
    """从映射中按 target_chars 顺序提取坐标，失败返回 None."""
    missing = [t for t in target_chars if t not in word_positions]
    if missing:
      logger.info(f"目标文字缺失: {missing}")
      return None
    result: list[tuple[int, int]] = []
    wp = {k: list(v) for k, v in word_positions.items()}
    for t in target_chars:
      pos = sorted(wp[t], key=lambda p: p[0])[0]
      wp[t].pop(0)
      if not wp[t]:
        del wp[t]
      result.append(pos)
    return result

  # ==================== 预处理 ====================

  def _preprocess(self, image_bytes: bytes) -> bytes:
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
      raise ValueError("无法解码图片数据")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    denoised = cv2.medianBlur(gray, 3)
    binary = cv2.adaptiveThreshold(
        denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
    _, encoded = cv2.imencode(".png", binary)
    return encoded.tobytes()

  @staticmethod
  def _save_debug_image(image_bytes: bytes, name: str) -> None:
    Path("logs").mkdir(exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    (Path("logs") / f"{name}_{ts}.png").write_bytes(image_bytes)

  def _parse_prompt(self, text: str) -> list[str]:
    cleaned = re.sub(
        r"请依次点击[：:]\s*|按顺序点击[：:]\s*|请依次点击\s*|按顺序点击\s*",
        "", text).strip()
    blocks = re.findall(r"[一-鿿]+", cleaned)
    targets: list[str] = []
    for block in blocks:
      if len(block) == 1:
        targets.append(block)
      else:
        if not targets:
          targets = list(block)[:6]
        break
    return targets

  def _bytes_to_numpy(self, image_bytes: bytes) -> np.ndarray:
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
      raise ValueError("无法解码图片数据")
    return img


def solve_captcha(image_bytes: bytes, prompt_text: str) -> list[tuple[int, int]]:
  return CaptchaSolver().solve(image_bytes, prompt_text)


if __name__ == "__main__":
  import sys
  from glm_buy.logger import setup_logging
  setup_logging()
  if len(sys.argv) < 3:
    print("用法: python -m glm_buy.verify.solver <图片路径> <提示语>")
    sys.exit(1)
  with open(sys.argv[1], "rb") as f:
    img_bytes = f.read()
  clicks = CaptchaSolver().solve(img_bytes, sys.argv[2])
  print(f"\n成功！点击坐标: {clicks}" if clicks else "\n识别失败")
