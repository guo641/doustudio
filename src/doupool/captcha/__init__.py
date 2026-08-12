"""
v0.3.0:豆包 aegis 人机验证自动通关。

豆包最近(2026-08)新增「拖动下方图片到上方轮廓」类拼图验证 —— 用户截图
显示是动物/物品图案,具体内容 aegis 随机变换。风控触发条件主要是「冷启动」:
新会话/IP 第一次请求 → 触发拖拽验证;同会话跑通后一段时间内不再触发。

策略:接图鉴打码平台(ttshitu.com) HTTP API,用 typeid 27 坐标点选
(覆盖「拖动对应物品到对应轮廓」场景) + 兜底 typeid 33 单缺口滑块
(覆盖传统横向拼图)。检测 → 截图 → 打码 → 模拟人拖拽 → 等待 aegis
校验结果。失败 3 次 → 抛 AegisCaptchaFailed。

账号 / 密码来源(优先级):
  1. 环境变量 DOUSTUDIO_TTSHITU_USERNAME / _PASSWORD
  2. SQLite settings(键 ttshitu_username / ttshitu_password)
都不存在 → client 是 no-op,solver 直接抛 DisabledByConfig 让上层降级。
"""
from __future__ import annotations

from .ttshitu_client import (
    TtshituCaptchaClient,
    TtshituSolve,
    TtshituError,
    TtshituDisabled,
)
from .solver import (
    CaptchaKind,
    detect_aegis_captcha,
    solve_aegis_captcha,
    human_like_drag,
    AegisCaptchaFailed,
    AegisCaptchaDisabled,
)
from .config import CaptchaCredentials, load_credentials, credentials_present


__all__ = [
    "TtshituCaptchaClient",
    "TtshituSolve",
    "TtshituError",
    "TtshituDisabled",
    "CaptchaKind",
    "detect_aegis_captcha",
    "solve_aegis_captcha",
    "human_like_drag",
    "AegisCaptchaFailed",
    "AegisCaptchaDisabled",
    "CaptchaCredentials",
    "load_credentials",
    "credentials_present",
]