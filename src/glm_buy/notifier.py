"""
系统通知与音频提醒模块（macOS 专用）.
对应 JS 版的 notify() 和 playAlert() 函数.
JS 版使用浏览器 Notification API 和 Web Audio API，Python 版使用 macOS 系统命令.
"""

import asyncio
import subprocess  # 调用系统命令

from loguru import logger


def notify(title: str, body: str) -> None:
  """
  发送 macOS 系统通知（右上角弹窗）.
  底层调用: osascript -e 'display notification "..." with title "..."'
  """
  try:
    subprocess.run(
        [
            "osascript",  # macOS AppleScript 解释器
            "-e",         # 执行一行脚本
            f'display notification "{body}" with title "{title}"',
        ],
        capture_output=True,  # 不显示命令输出
        timeout=5,            # 5秒超时
    )
    logger.info(f"通知: {title} - {body}")
  except Exception as e:
    logger.debug(f"通知发送失败: {e}")


def play_alert() -> None:
  """
  播放系统提示音（3 次"玻璃"音效）.
  底层调用: afplay /System/Library/Sounds/Glass.aiff
  """
  try:
    for _ in range(3):  # 连续播放 3 次
      subprocess.run(
          ["afplay", "/System/Library/Sounds/Glass.aiff"],  # macOS 音频播放命令
          capture_output=True,
          timeout=3,
      )
  except Exception as e:
    # 音频播放失败时的兜底方案：终端响铃
    logger.debug(f"音频播放失败: {e}")
    print("\a")  # ASCII 响铃字符


async def async_notify(title: str, body: str) -> None:
  """
  异步包装器：在线程池中执行同步的 notify().
  避免阻塞 asyncio 事件循环.
  """
  await asyncio.to_thread(notify, title, body)


async def async_play_alert() -> None:
  """
  异步包装器：在线程池中执行同步的 play_alert().
  避免阻塞 asyncio 事件循环.
  """
  await asyncio.to_thread(play_alert)
