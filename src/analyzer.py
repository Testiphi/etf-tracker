"""
AI 分析模块 —— 调用 DeepSeek API 对基金净值、大盘指数和黄金行情进行综合分析

启用条件：
  1. config/settings.yaml 中 ai_analysis.enabled = true
  2. GitHub Secrets 中设置了 AI_API_KEY
"""

import json
import requests
from datetime import datetime, timezone, timedelta

from .config import Config
from .fetchers.base import FundDataPoint

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


def analyze(
    cfg: Config,
    results: dict[str, FundDataPoint],
    index_results: dict[str, FundDataPoint] | None = None,
    risk_report: dict | None = None,
    portfolio_detail: dict | None = None,
) -> str | None:
    """调用 DeepSeek 对当日数据进行综合分析"""
    if not cfg.ai_enabled:
        return None

    if not cfg.ai_api_key:
        return "AI 分析已启用但未配置 API Key（请设置 GitHub Secret: AI_API_KEY）"

    # 构造 prompt（含基金 + 指数 + 黄金 + 风险 + 组合明细）
    prompt = _build_prompt(cfg, results, index_results or {}, risk_report, portfolio_detail)

    # 调用 DeepSeek API
    headers = {
        "Authorization": f"Bearer {cfg.ai_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是一位专业的基金投资分析师，语言简洁、客观。回答问题使用中文。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 1000,
    }

    try:
        resp = requests.post(
            DEEPSEEK_URL,
            json=payload,
            headers=headers,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        print("  AI 分析完成")
        return content.strip()
    except requests.exceptions.Timeout:
        print("  AI API 请求超时")
        return "AI 分析请求超时，请稍后重试。"
    except requests.exceptions.HTTPError as e:
        if resp.status_code == 401:
            return "AI API Key 无效，请检查 GitHub Secret: AI_API_KEY"
        elif resp.status_code == 429:
            return "AI API 调用频率过高/额度用尽，请稍后再试。"
        return f"AI API 请求失败 (HTTP {resp.status_code}): {e}"
    except (KeyError, json.JSONDecodeError) as e:
        return f"AI API 响应解析失败: {e}"
    except Exception as e:
        return f"AI 分析异常: {e}"


def _build_prompt(
    cfg: Config,
    results: dict[str, FundDataPoint],
    index_results: dict[str, FundDataPoint],
    risk_report: dict | None = None,
    portfolio_detail: dict | None = None,
) -> str:
    """构造发给 DeepSeek 的综合分析 prompt"""
    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.strftime("%Y-%m-%d")

    lines = [
        f"今天是 {today}，以下是今日完整的市场数据：",
        "",
    ]

    # --- 基金净值 ---
    lines.extend([
        "### 基金净值",
        "",
        "| 基金名称 | 代码 | 单位净值 | 日涨跌幅 |",
        "|---------|------|---------|:-------:|",
    ])
    for fund in cfg.funds:
        code = fund["code"]
        name = fund["name"]
        point = results.get(code)
        if point:
            lines.append(
                f"| {name} | {code} | {point.net_value:.4f} | {point.daily_change:+.2f}% |"
            )
        else:
            lines.append(f"| {name} | {code} | — | 无数据 |")

    lines.append("")

    # --- 大盘指数 ---
    if cfg.indices:
        lines.extend([
            "### 大盘指数",
            "",
            "| 指数 | 收盘价 | 涨跌幅 | 成交量 |",
            "|------|:-----:|:-----:|:-----:|",
        ])
        for idx_cfg in cfg.indices:
            code = idx_cfg["code"]
            name = idx_cfg["name"]
            point = index_results.get(code)
            if point:
                lines.append(
                    f"| {name} | {point.net_value:.2f} | {point.daily_change:+.2f}% | {point.volume:.0f} |"
                )
            else:
                lines.append(f"| {name} | — | 无数据 | — |")

    lines.append("")

    # --- 黄金 ---
    commodity_prefix = "commodity:"
    gold_items = {k: v for k, v in index_results.items() if k.startswith(commodity_prefix)}
    if gold_items:
        lines.extend([
            "### 黄金行情",
            "",
            "| 品种 | 价格 | 涨跌幅 | 成交量 |",
            "|------|:---:|:-----:|:-----:|",
        ])
        for cfg_item in cfg.commodities:
            full_code = f"{commodity_prefix}{cfg_item['code']}"
            point = gold_items.get(full_code)
            if point:
                lines.append(
                    f"| {point.name} | {point.net_value:.2f} | {point.daily_change:+.2f}% | {point.volume:.0f} |"
                )
            else:
                lines.append(f"| {cfg_item['name']} | — | 无数据 | — |")

    # ── 组合风险数据 ────────────────────────────────
    if risk_report:
        lines.extend([
            "### 组合风险指标",
            "",
            f"- 组合今日收益率: {risk_report['portfolio_return']:+.2f}%",
            f"- 累积收益率（近期）: {risk_report['cumulative_return']:+.2f}%",
            f"- 最大回撤: {risk_report['max_drawdown']:.2f}%",
            f"- 日波动率: {risk_report['volatility']:.2f}%",
            f"- {risk_report['benchmark_name']} 同期收益: {risk_report['benchmark_return']:+.2f}%",
            f"- 组合 vs 基准: {risk_report['cumulative_return'] - risk_report['benchmark_return']:+.2f}%",
            "",
        ])

    # ── 组合盈亏明细 ────────────────────────────────
    if portfolio_detail:
        lines.extend([
            "### 组合持仓与盈亏",
            "",
            f"- 总投入: ¥{portfolio_detail['total_cost']:.2f}",
            f"- 总市值: ¥{portfolio_detail['total_value']:.2f}",
            f"- 总盈亏: ¥{portfolio_detail['total_pnl']:+.2f} ({portfolio_detail['total_pnl_pct']:+.2f}%)",
            "",
        ])
        lines.append("各基金明细：")
        for h in portfolio_detail.get("holdings", []):
            lines.append(
                f"  - {h['name']}: 份数={h['shares']:.4f}"
                f" 成本=¥{h['cost']:.2f}"
                f" 市值=¥{h['value']:.2f}"
                f" 盈亏={h['pnl']:+.2f} ({h['pnl_pct']:+.2f}%)"
            )
        lines.append("")

    # ── 定投计划 ────────────────────────────────────
    if cfg.fixed_investment_daily:
        lines.extend([
            "### 定投计划",
            "",
            "| 基金 | 每日金额 |",
            "|------|:-------:|",
        ])
        for plan in cfg.fixed_investment_daily:
            lines.append(f"| {plan['name']}（{plan['code']}） | {plan['amount']}元 |")
        lines.append("")

    lines.append("")
    lines.append("---")
    lines.append("")

    # 用户自定义 prompt
    if cfg.ai_prompt:
        lines.append(cfg.ai_prompt)
    else:
        lines.extend([
            "请根据以上数据给出综合分析：",
            "1. 今日基金净值整体表现如何？涨跌原因可能是什么？",
            "2. 对应宽基指数（科创50、中证A500、标普500、纳斯达克100）今日表现如何？成交量有无异常？",
            "3. 黄金行情今日如何？对避险情绪有何提示？",
            "4. 短期趋势判断（1-2周维度）和调仓建议",
        ])

    return "\n".join(lines)
