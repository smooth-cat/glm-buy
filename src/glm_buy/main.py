"""
程序入口 — GLM Coding Sniper (Python + Playwright 版本).
启动浏览器 → 初始化各模块 → 进入倒计时 → 到点抢购.

运行方式:
    uv run python -m glm_buy.main

配置修改: 编辑 src/glm_buy/config.py 中的 build_config() 函数.
"""

import asyncio

from loguru import logger

from glm_buy.config import config  # 模块级单例，所有配置项集中在此
from glm_buy.logger import setup_logging
from glm_buy.scheduler import Scheduler


async def main() -> None:
  """主异步函数 — 初始化并启动调度器."""
  setup_logging()

  scheduler = Scheduler(config)

  try:
    await scheduler.run()
  except KeyboardInterrupt:
    logger.info("用户中断，正在退出...")
  except Exception as e:
    logger.exception(f"运行异常: {e}")
  finally:
    if scheduler.browser:
      await scheduler.browser.stop()


if __name__ == "__main__":
  asyncio.run(main())
