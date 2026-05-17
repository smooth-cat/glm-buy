"""
日志系统初始化 — 使用 loguru 实现终端彩色输出 + 按日轮转文件日志.
对应 JS 版本中的 log() 和 setStatus() 函数，但改用专业日志库.
"""

import sys
from pathlib import Path

from loguru import logger  # 第三方日志库，比标准库 logging 更简洁


def setup_logging(log_dir: str = "logs") -> None:
  """初始化 loguru：终端彩色输出 + 每日轮转日志文件."""
  # 移除 loguru 默认的 handler，从头配置
  logger.remove()

  # 终端输出：带颜色、时间精确到毫秒
  logger.add(
      sys.stderr,  # 输出到标准错误流（终端可见）
      format=(
          "<green>{time:HH:mm:ss.SSS}</green> | "  # 绿色时间戳
          "<level>{level: <8}</level> | "            # 日志级别（左对齐8字符）
          "<level>{message}</level>"                  # 日志内容
      ),
      level="INFO",   # 终端只显示 INFO 及以上级别
      colorize=True,  # 开启 ANSI 颜色
  )

  # 确保日志目录存在
  Path(log_dir).mkdir(parents=True, exist_ok=True)
  # 文件输出：完整时间戳，每日午夜自动轮转
  logger.add(
      Path(log_dir) / "glm-sniper-{time:YYYY-MM-DD}.log",  # 按日期命名
      format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {message}",
      level="DEBUG",      # 文件记录所有级别（含 DEBUG）
      rotation="00:00",   # 每天午夜 00:00 创建新文件
      retention="7 days", # 保留最近 7 天的日志
      encoding="utf-8",
  )

  # 启动确认
  logger.info("日志系统初始化完成")
