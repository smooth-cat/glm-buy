# GLM Buy

每天 10:00 自动在智谱 AI 开放平台抢购 GLM Coding Plan Pro 订阅的 Python 脚本。

基于 Playwright 浏览器自动化 + PaddleOCR 验证码识别，模拟真实鼠标操作完成购买流程。

## 环境要求

| 项目 | 说明 |
|------|------|
| 操作系统 | macOS |
| Python | >= 3.12 |
| 网络 | 能访问 open.bigmodel.cn |

> Windows/Linux 需自行替换通知和音频部分（`notifier.py` 中的 `osascript` 和 `afplay`）。

## 安装步骤

### 第 1 步：安装 uv（Python 包管理器）

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**为什么？** `uv` 是 Rust 写的 Python 包管理器，比 `pip` 快 10-100 倍。它会自动创建虚拟环境、锁定依赖版本，避免"在我机器上能跑"的问题。

**这条命令做了什么？** `curl` 下载官方安装脚本 → `| sh` 执行它 → 把 `uv` 二进制放到 `~/.local/bin/uv`。安装完成后关掉终端重新打开使其生效。

### 第 2 步：克隆项目

```bash
git clone <仓库地址>
cd glm-buy
```

### 第 3 步：安装 Python 依赖

```bash
uv sync
```

**为什么？** 这个命令会读取项目根目录的 `pyproject.toml`，自动做三件事：
1. 创建项目的隔离虚拟环境 `.venv/`（不污染系统 Python）
2. 下载并安装所有依赖（playwright、paddleocr、loguru 等）
3. 生成 `uv.lock` 锁定精确版本（保证所有人装的版本一致）

### 第 4 步：安装 Playwright 浏览器

```bash
uv run playwright install chromium
```

**为什么？** Playwright 需要真实浏览器来模拟用户操作。它会下载一个独立的 Chromium（和你的 Chrome 互不影响），约 250MB。`uv run` 确保在项目虚拟环境中执行，而非全局。

**为什么是 Chromium？** Chromium 支持"持久化上下文"——Cookie 和登录态保存到 `browser_profile/` 目录，下次启动自动恢复，无需重新登录。

### 第 5 步：配置抢购目标

编辑 `src/glm_buy/main.py`，找到 `build_config()` 函数：

```python
def build_config() -> Config:
    return Config(
        plan_priority=[
            PlanPriority(plan="lite", billing_period="quarterly"),  # 首选
            # PlanPriority(plan="pro", billing_period="quarterly"),   # 候补（去掉井号启用）
        ],
        target_hour=10,      # 几点开抢
        target_minute=0,     # 几分开抢（默认 10:00）
        advance_ms=200,      # 提前 200 毫秒开始点击（补偿网络延迟）
        max_retries=300,     # 最多重试 300 次 = 30 秒
        headless=False,      # False = 可见窗口（鼠标模拟需要看到浏览器）
    )
```

| 配置项 | 含义 | 默认值 |
|--------|------|--------|
| `plan` | 套餐名：`lite`(49元) / `pro`(149元) / `max`(469元) | `lite` |
| `billing_period` | 计费周期：`monthly`(包月) / `quarterly`(包季) / `yearly`(包年) | `quarterly` |
| `target_hour/minute` | 每天几点几分抢购 | 10:00 |
| `advance_ms` | 提前多少毫秒开始点击 | 200 |
| `max_retries` | 最大重试次数（×100ms = 总时长） | 300 |

## 运行

### 抢购主程序

```bash
uv run python -m glm_buy.main
```

**首次运行会做什么？**
1. 启动 Chromium 浏览器 → 打开 `open.bigmodel.cn/glm-coding`
2. **你需要手动登录一次**（如果未登录）
3. 登录态自动保存到 `browser_profile/`，以后不用再登
4. 终端显示倒计时 → 到 10:00 自动抢购

**之后每次运行：** 浏览器自动恢复登录态，直接进入倒计时等待抢购。

### 验证码识别测试（调试用）

```bash
uv run python -m glm_buy.verify.demo
```

在腾讯云验证码演示页上测试验证码识别和自动点击流程。

### 离线识别测试（本地验证码图片）

```bash
uv run python -m glm_buy.verify.solver logs/captcha_debug.png "请依次点击：我 爱 心"
```

## 项目结构

```
glm-buy/
├── README.md                      # 本文档
├── pyproject.toml                 # 项目配置（依赖列表、Python 版本要求）
├── uv.lock                        # 依赖锁文件（保证版本一致）
├── .gitignore                     # Git 忽略规则
├── 项目结构.md                     # 详细项目结构说明
├── 正向流程.md                     # 购买正向流程函数调用链
├── glm-coding-sniper.user.js      # 原版 JS 油猴脚本（参考）
├── src/glm_buy/
│   ├── main.py                    # ★ 程序入口
│   ├── config.py                  # 配置数据类
│   ├── logger.py                  # 日志初始化（终端彩色 + 文件轮转）
│   ├── scheduler.py               # 核心调度器（倒计时 → 抢购循环）
│   ├── browser.py                 # 浏览器管理 + API 响应拦截
│   ├── dom_reader.py              # DOM 只读检查（找按钮/弹窗）
│   ├── mouse.py                   # 鼠标模拟（贝塞尔曲线移动 + 点击）
│   ├── product.py                 # 商品 ID 识别与缓存
│   ├── api.py                     # 直接 HTTP API 调用
│   ├── notifier.py                # macOS 系统通知 + 音频
│   └── verify/                    # 验证码自动识别模块
│       ├── solver.py              # PaddleOCR 文字识别
│       └── demo.py                # 独立演示脚本
├── browser_profile/               # 浏览器持久化目录（登录态存放）
├── logs/                          # 日志文件
└── .venv/                         # Python 虚拟环境（uv 自动创建）
```

## 常见问题

**Q: 浏览器启动超时？**
删除 `browser_profile/` 目录重试。这个目录可能被上一次异常退出锁住。

**Q: 验证码识别不准？**
`logs/` 下会保存每次识别的截图，可以手动运行离线测试调参。

**Q: 想切换套餐？**
编辑 `main.py` 中的 `plan_priority` 列表，支持多候补自动切换。

**Q: 提示 "未找到确定按钮" 但识别成功？**
说明文字点击正确，但确认按钮的位置和预期不同。查看 `logs/` 中的日志，搜索 `frame[2]` 确认按钮文字实际在哪。

## 依赖体积

| 依赖 | 用途 | 约体积 |
|------|------|--------|
| Playwright Chromium | 浏览器自动化 | 250MB |
| PaddleOCR + PaddlePaddle | 验证码 OCR | 500MB |
| OpenCV | 图像处理 | 60MB |
| loguru / httpx | 日志 / HTTP | < 5MB |
