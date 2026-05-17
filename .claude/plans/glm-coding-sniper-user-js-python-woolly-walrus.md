# 回退：PaddleOCR DB 检测 + 简化拆字

## Context

OpenCV contour 检测噪声太多（13-27 个框 vs 实际 3-4 个），效果不如 PaddleOCR DB 检测器。

## 方案

保留 PaddleOCR DB 检测的 4 个框结果（之前验证有效），只改拆字方式：
- DB 检测到合并框时，不投影拆字，直接按字符数均分框宽
- 每个字符取原始文字对应位置的字符，不需要重新 OCR

`_detect_chars_opencv` → 删除
`_ocr_fallback` → 升级为主逻辑（PaddleOCR 检测）
拆字 → 只保留 `_split_merged_chars`，用均分替代投影

## 改动

- `solver.py` — 删除 `_detect_chars_opencv`，`_detect_and_recognize` 直接走 PaddleOCR
