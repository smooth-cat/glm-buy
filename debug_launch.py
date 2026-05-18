"""
VS Code 调试入口 — 自动算目标时间（当前 + 8s），设环境变量，启动主程序.

用法: 在 VS Code 中选 "GLM 本地测试" 配置，按 F5.
     需先确保 test_server.py 已运行（或用 preLaunchTask 自动启动）.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

# 确保 src/ 在 sys.path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

DELAY = 8  # 几秒后开抢，和 test-buy.sh 默认 5 不同，留点余量给断点

target = datetime.now() + timedelta(seconds=DELAY)
os.environ["GLM_BUY_URL"] = "http://127.0.0.1:8765/test_page.html"
os.environ["GLM_BUY_API_BASE"] = "http://127.0.0.1:8765"
os.environ["GLM_BUY_TARGET_HOUR"] = str(target.hour)
os.environ["GLM_BUY_TARGET_MINUTE"] = str(target.minute)
os.environ["GLM_BUY_TARGET_SECOND"] = str(target.second)
os.environ["GLM_BUY_HEADLESS"] = "0"  # 调试时用有头模式

print(f"目标时间: {target.strftime('%H:%M:%S')} ({DELAY}s 后)")
print(f"页面 URL: {os.environ['GLM_BUY_URL']}")
print(f"API URL:  {os.environ['GLM_BUY_API_BASE']}")
print("")

from glm_buy.main import main

asyncio.run(main())
