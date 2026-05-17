"""
通知与音频模块 — 当前为空实现（跨平台兼容）.

后续可按需补充各平台实现:
  macOS:   osascript + afplay
  Windows: win10toast + winsound
  Linux:   notify-send + paplay
"""

from loguru import logger


def notify(title: str, body: str) -> None:
  """发送系统通知（当前为空实现，仅打日志）."""
  logger.info(f"[通知] {title}: {body}")


def play_alert() -> None:
  """播放提示音（当前为空实现，仅打日志）."""
  logger.info("[音频] 提示音")


# 异步包装器（保持接口兼容）
import asyncio

async def async_notify(title: str, body: str) -> None:
  await asyncio.to_thread(notify, title, body)


async def async_play_alert() -> None:
  await asyncio.to_thread(play_alert)
