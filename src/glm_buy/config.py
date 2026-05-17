"""
配置模块 — 所有抢购参数和常量映射表的唯一来源.

修改 build_config() 即可定制抢购行为.
其他文件通过 from glm_buy.config import config 引用当前配置.
"""

from dataclasses import dataclass, field


@dataclass
class PlanPriority:
  """单个套餐的优先级别：套餐名 + 计费周期."""
  plan: str            # 'lite' | 'pro' | 'max'
  billing_period: str  # 'monthly' | 'quarterly' | 'yearly'


@dataclass
class Config:
  """
  全局配置 — 所有参数都有默认值.
  用法: Config(plan_priority=[PlanPriority(plan="lite", billing_period="quarterly")])
  """
  plan_priority: list[PlanPriority] = field(default_factory=lambda: [
      PlanPriority(plan="lite", billing_period="quarterly"),
  ])
  target_hour: int = 10
  target_minute: int = 0
  target_second: int = 0
  advance_ms: int = 200
  retry_interval: int = 100
  max_retries: int = 300
  auto_refresh: bool = True
  auto_refresh_seconds_before: int = 10
  headless: bool = False
  user_data_dir: str = "browser_profile"
  purchase_url: str = "https://open.bigmodel.cn/glm-coding"


def build_config() -> Config:
  """
  构建配置对象 — 在这里修改参数来定制抢购行为.

  Plan 选项:  'lite' | 'pro' | 'max'
  Period 选项: 'monthly' | 'quarterly' | 'yearly'
  列表顺序即优先级：第一个是首选，后续是候补.
  """
  return Config(
      plan_priority=[
          PlanPriority(plan="lite", billing_period="quarterly"),
          # PlanPriority(plan="pro", billing_period="quarterly"),  # 候补
      ],
      target_hour=10,
      target_minute=0,
      target_second=0,
      advance_ms=200,
      retry_interval=100,
      max_retries=300,
      auto_refresh=True,
      auto_refresh_seconds_before=10,
      headless=False,
  )


# ---- 模块级单例 — 其他文件 from glm_buy.config import config 即可引用 ----
config = build_config()


# ---- 常量映射表（DOM 匹配 / 显示用） ----

PERIOD_LABELS = {
    "monthly": "包月",
    "quarterly": "包季",
    "yearly": "包年",
}

PLAN_KEYWORDS = {
    "lite": ["lite", "Lite", "LITE", "基础", "轻量"],
    "pro": ["pro", "Pro", "PRO", "专业", "进阶"],
    "max": ["max", "Max", "MAX", "旗舰", "高级"],
}

PERIOD_TAB_KEYWORDS = {
    "monthly": {"match": "包月", "exclude": ["包季", "包年"], "label": "连续包月"},
    "quarterly": {"match": "包季", "exclude": ["包月", "包年"], "label": "连续包季"},
    "yearly": {"match": "包年", "exclude": ["包月", "包季"], "label": "连续包年"},
}

# ---- 验证码 DOM 选择器（腾讯云验证码页面） ----

CAPTCHA_SELECTORS = {
    # 腾讯云验证码演示页
    "demo_page_url": "https://cloud.tencent.com/product/captcha",
    # 验证码类型选择容器
    "box_container": ".captcha-box-second",        
    # 文字点选验证的标签文本
    "text_verify_tab": "文字点选验证",               
    # 立即体验按钮文本
    "experience_btn": "立即体验",                    
              

    # 验证码交互
    # 刷新按钮
    "action_refresh": ".tc-action--refresh",        
    # 操作区容器
    "opera": ".tc-opera",                           
    # 加载中的状态类
    "opera_loading": ".tc-opera .show-loading",     
    # 确认按钮文字
    "confirm_btn_text": "确定",                      
}
