"""配置解析模块 —— 读取 settings.yaml + 环境变量"""

import os
import yaml
from pathlib import Path


class Config:
    """全局配置，加载一次后以属性方式访问"""

    def __init__(self, config_path: str | None = None):
        if config_path is None:
            config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"

        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        # --- 定时 ---
        schedule = raw.get("schedule", {})
        self.schedule_time: str = schedule.get("time", "20:00")
        self.schedule_timezone: str = schedule.get("timezone", "Asia/Shanghai")

        # --- 基金列表 ---
        self.funds: list[dict] = raw.get("funds", [])
        if not self.funds:
            raise ValueError("⚠️ settings.yaml 中 funds 列表为空，请至少添加一支基金")
        # 为未设定 weight 的基金默认 weight=1，便于组合计算
        for f in self.funds:
            f.setdefault("weight", 1)

        # --- 大盘指数 ---
        self.indices: list[dict] = raw.get("indices", [])
        self.commodities: list[dict] = raw.get("commodities", [])

        # --- AI 分析 ---
        ai = raw.get("ai_analysis", {}) or {}
        self.ai_enabled: bool = ai.get("enabled", False)
        self.ai_provider: str = ai.get("provider", "deepseek")
        self.ai_api_key: str = os.environ.get(ai.get("api_key_env", ""), "")
        self.ai_prompt: str = ai.get("prompt", "")

        # --- 验证 ---
        val = raw.get("validation", {}) or {}
        self.min_sources: int = val.get("min_sources", 2)
        self.max_retries: int = val.get("max_retries", 3)
        self.retry_cooldown: int = val.get("retry_cooldown", 10)

        # --- 邮件 ---
        mail = raw.get("email", {}) or {}
        self.smtp_server: str = mail.get("smtp_server", "smtp.office365.com")
        self.smtp_port: int = mail.get("smtp_port", 587)
        self.smtp_use_tls: bool = mail.get("use_tls", True)
        self.mail_from: str = os.environ.get(mail.get("from_env", ""), "")
        self.mail_password: str = os.environ.get(mail.get("password_env", ""), "")
        self.mail_to: str = mail.get("to", "") or self.mail_from
        self.mail_subject_prefix: str = mail.get("subject_prefix", "[基金日报]")

    def validate_email_config(self):
        """检查邮件配置是否完整"""
        missing = []
        if not self.mail_from:
            missing.append("OUTLOOK_EMAIL")
        if not self.mail_password:
            missing.append("OUTLOOK_PASSWORD")
        if missing:
            raise RuntimeError(
                f"❌ 邮件配置不完整，请设置 GitHub Secrets: {', '.join(missing)}"
            )
