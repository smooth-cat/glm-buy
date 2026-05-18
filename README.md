# GLM Buy

每天 10:00 自动在智谱 AI 开放平台抢购 GLM Coding Plan Pro 订阅的 Python 脚本。

基于 Playwright 浏览器自动化 + PaddleOCR 验证码识别，模拟真实鼠标操作完成购买流程。

## 环境要求

| 项目 | 说明 |
|------|------|
| 操作系统 | macOS |
| Python | >= 3.12 |
| 网络 | 能访问 open.bigmodel.cn |

## 安装步骤

### 第 1 步：安装 uv（Python 包管理器）

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**为什么？** `uv` 是 Rust 写的 Python 包管理器，比 `pip` 快 10-100 倍。它会自动创建虚拟环境、锁定依赖版本，避免"在我机器上能跑"的问题。

### 第 2 步：安装 Python 依赖

```bash
uv sync
```

### 第 3 步：安装 Playwright 浏览器

```bash
uv run playwright install chromium
```

**为什么？** Playwright 需要真实浏览器来模拟用户操作。它会下载一个独立的 Chromium（和你的 Chrome 互不影响），约 250MB。`uv run` 确保在项目虚拟环境中执行，而非全局。

### 第 4 步：配置抢购目标

编辑 `src/glm_buy/config.py`，找到 `build_config()` 函数：

```python
def build_config() -> Config:
    return Config(
        plan_priority=[
            PlanPriority(plan="lite", billing_period="quarterly"),  # 首选 ⭐
            # PlanPriority(plan="pro", billing_period="quarterly"), # 候补 ⭐
        ],
       ...
    )
```

| 配置项 | 含义 | 默认值 |
|--------|------|--------|
| ⭐`plan` | 套餐名：`lite`(49元) / `pro`(149元) / `max`(469元) | `lite` |
| ⭐`billing_period` | 计费周期：`monthly`(包月) / `quarterly`(包季) / `yearly`(包年) | `quarterly` |
| `target_hour/minute` | 每天几点几分抢购 | 10:00 |
| `advance_ms` | 提前多少毫秒开始点击 | 200 |
| `max_retries` | 最大重试次数（×100ms = 总时长） | 300 |

## 运行

### 抢购主程序

```bash
uv run python -m glm_buy.main
```

## 注意事项

1. ⭐ **需要手动登录一次** （如果未登录）
2. ⭐  **如果文字验证码未通过，手动刷新页面让抢购流程重新进行** 
3. ⭐ **如果抢购时出现金额为空的二维码，手动刷新页面让抢购流程重新进行** 
4. ⭐ **截图屏幕闪烁不影响使用**
   Playwright headed 模式截图调用 macOS `CGWindowListCreateImage`，是已知问题，无法根治

### **核心能力**

- 倒计时到点自动触发射击循环（每 100ms 一次，最多 300 次）
- 页面 API 响应拦截：在 10:00-10:02 窗口内将 soldOut 改为 false
- 检测到验证码弹窗时 **自动识别并点击**（PaddleOCR + 鼠标模拟）
- 页面刷新后自动重新触发抢购
- 套餐售罄时自动切换到候补套餐
- 页面异常时自动恢复（刷新/跳转）

### 验证码识别测试（调试用）

```bash
uv run python -m glm_buy.verify.demo
```

在腾讯云验证码演示页上测试自动识别和点击流程。

### 离线识别测试

```bash
uv run python -m glm_buy.verify.solver logs/captcha_debug.png "请依次点击：我 爱 心"
```

### 查看流程图

```bash
open 流程.html
```

## 项目结构

```
glm-buy/
├── README.md                      # 本文档
├── pyproject.toml                 # 项目配置（依赖列表、Python 版本要求）
├── uv.lock                        # 依赖锁文件（保证版本一致）
├── .gitignore                     # Git 忽略规则
├── 流程.html                      # Mermaid 完整流程图（浏览器打开）
├── 项目结构.md                     # 详细项目结构说明
├── 正向流程.md                     # 购买正向流程函数调用链
├── glm-coding-sniper.user.js      # 原版 JS 油猴脚本（参考）
├── src/glm_buy/
│   ├── main.py                    # ★ 程序入口
│   ├── config.py                  # ★ 全部配置项（修改抢购参数改这里）
│   ├── logger.py                  # 日志初始化（终端彩色 + 文件轮转）
│   ├── scheduler.py               # 核心调度器（倒计时 → 抢购循环 → 验证码 → 恢复）
│   ├── browser.py                 # 浏览器管理 + API 响应拦截 + 页面刷新检测
│   ├── dom_reader.py              # DOM 只读检查（找按钮/弹窗）
│   ├── mouse.py                   # 鼠标模拟（贝塞尔曲线移动 + 点击）
│   ├── product.py                 # 商品 ID 识别与缓存
│   ├── api.py                     # 直接 HTTP API 调用
│   ├── notifier.py                # macOS 系统通知 + 音频
│   └── verify/                    # 验证码自动识别模块
│       ├── solver.py              # PaddleOCR 检测 + 识别 + 拆字
│       └── demo.py                # 独立演示脚本 + solve_captcha() 复用接口
├── browser_profile/               # 浏览器持久化目录（登录态存放）
├── logs/                          # 日志文件 + 截图
└── .venv/                         # Python 虚拟环境（uv 自动创建）
```

## 常见问题

**Q: 浏览器启动超时？**
删除 `browser_profile/` 目录重试。可能被上一次异常退出锁住。

**Q: 验证码识别不准？**
`logs/` 下会保存每次识别的截图（`captcha_attempt{1,2,3}.png`），可离线测试调参。OCR 参数在 `solver.py` 的 `__init__` 中。

**Q: 想切换套餐？**
编辑 `config.py` 中的 `plan_priority` 列表，支持多候补自动切换。

**Q: 验证码平台换了怎么办？**
编辑 `config.py` 中的 `CAPTCHA_SELECTORS` 字典，修改 DOM 选择器和 URL。

**Q: 屏幕截图闪烁？**
Playwright headed 模式截图调用 macOS `CGWindowListCreateImage`，是已知问题，无法根治。可加 `--disable-blink-features=AutomationControlled` 减轻。

## 依赖体积

| 依赖 | 用途 | 约体积 |
|------|------|--------|
| Playwright Chromium | 浏览器自动化 | 250MB |
| PaddleOCR + PaddlePaddle | 验证码 OCR | 500MB |
| OpenCV | 图像处理 | 60MB |
| loguru / httpx | 日志 / HTTP | < 5MB |
