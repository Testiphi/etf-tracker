"""
日报生成器 ── 生成美观的 Markdown 日报

支持基金净值 + 大盘指数 + 商品行情三板块。
"""

from datetime import datetime, timezone, timedelta
from .config import Config
from .fetchers.base import FundDataPoint


def get_beijing_time() -> datetime:
    """获取当前北京时间"""
    return datetime.now(timezone(timedelta(hours=8)))


_FUND_SOURCE_ICONS = {
    "eastmoney-lsjz": "📡",
    "eastmoney-fundgz": "📡",
    "sina-fund-api": "🔄",
    "ttjj-page": "🌐",
    "sina-etf-hq": "🔄",
}


def _format_change(change: float) -> str:
    """格式化涨跌幅 + 颜色符号"""
    direction = "🟢" if change >= 0 else "🔴"
    return f"{direction} {change:+.2f}%"


def _fmt_pct(val: float) -> str:
    """格式化百分比（含颜色符号）"""
    direction = "🟢" if val >= 0 else "🔴"
    return f"{direction} {val:+.2f}%"


def _fmt_risk(val: float) -> str:
    """格式化风险指标（回撤用红色标注）"""
    if val >= 0:
        return "0.00%"
    return f"🔴 {val:.2f}%"


def _volume_str(vol: float) -> str:
    """格式化成交量，显示为可读形式"""
    if vol <= 0:
        return "—"
    if vol >= 100000000:
        return f"{vol / 100000000:.2f}亿"
    elif vol >= 10000:
        return f"{vol / 10000:.2f}万"
    else:
        return f"{vol:.0f}"


def generate_report(
    cfg: Config,
    results: dict[str, FundDataPoint],
    index_results: dict[str, FundDataPoint],
    ai_analysis: str | None,
    errors: str,
    risk_report: dict | None = None,
) -> str:
    """
    生成 Markdown 格式日报

    Args:
        cfg: 配置
        results: code → FundDataPoint 映射（基金/ETF）
        index_results: code→FundDataPoint 映射（指数+商品）
        ai_analysis: AI 分析文本
        errors: 错误汇总
    """
    now = get_beijing_time()
    today = now.strftime("%Y-%m-%d")
    weekday = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]

    lines = [
        f"## 📊 基金日报 — {today}（周{weekday}）",
        "",
        f"_生成时间：{now.strftime('%H:%M')} 北京时间_",
        "",
        "---",
        "",
        "### 📋 今日净值",
        "",
        "| 基金 | 代码 | 单位净值 | 日涨跌幅 | 对应指数 | 数据源 |",
        "|------|------|---------|:-------:|:--------:|:------:|",
    ]

    for fund in cfg.funds:
        code = fund["code"]
        name = fund["name"]
        point = results.get(code)

        if point:
            change_str = _format_change(point.daily_change)
            source_icon = _FUND_SOURCE_ICONS.get(point.source, "❓")
            # 找对应指数名
            lines.append(
                f"| {name} | `{code}` | {point.net_value:.4f} | {change_str} | {_match_index_name(name, index_results)} | {source_icon} {point.source} |"
            )
        else:
            lines.append(f"| {name} | `{code}` | — | ⚠️ 无数据 | — | ⛔ |")

    # ── 大盘指数板块 ──────────────────────────────────
    if cfg.indices and any(f"index:{i['code']}" in index_results or i["code"] in index_results for i in cfg.indices):
        lines.extend([
            "",
            "---",
            "",
            "### 📈 大盘行情",
            "",
            "| 指数 | 代码 | 收盘价 | 涨跌幅 | 成交量 | 数据源 |",
            "|------|------|:-----:|:-----:|:-----:|:------:|",
        ])
        for idx_cfg in cfg.indices:
            code = idx_cfg["code"]
            name = idx_cfg["name"]
            point = index_results.get(code)
            if point:
                vol_str = _volume_str(point.volume)
                source_icons = {
                    "eastmoney-index-hq": "📡",
                    "yahoo-finance": "🌍",
                }.get(point.source, "❓")
                lines.append(
                    f"| {name} | `{code}` | {point.net_value:.2f} | {_format_change(point.daily_change)} | {vol_str} | {source_icons} {point.source} |"
                )
            else:
                lines.append(f"| {name} | `{code}` | — | ⚠️ 无数据 | — | ⛔ |")

    # ── 黄金板块 ─────────────────────────────────────
    commodity_prefix = "commodity:"
    gold_items = {k: v for k, v in index_results.items() if k.startswith(commodity_prefix)}
    if gold_items:
        lines.extend([
            "",
            "---",
            "",
            "### 🥇 商品行情",
            "",
            "| 品种 | 代码 | 价格 | 涨跌幅 | 成交量 | 数据源 |",
            "|------|------|:----:|:-----:|:-----:|:------:|",
        ])
        for cfg_item in cfg.commodities:
            full_code = f"{commodity_prefix}{cfg_item['code']}"
            point = gold_items.get(full_code)
            if point:
                lines.append(
                    f"| {point.name} | `{cfg_item['code']}` | {point.net_value:.2f} | {_format_change(point.daily_change)} | {_volume_str(point.volume)} | 📡 {point.source} |"
                )
            else:
                lines.append(f"| {cfg_item['name']} | `{cfg_item['code']}` | — | ⚠️ 无数据 | — | ⛔ |")

    # ── 组合明细 ─────────────────────────────────────
    lines.extend([
        "",
        "---",
        "",
        "### ℹ️ 各基金详情",
        "",
    ])
    for fund in cfg.funds:
        code = fund["code"]
        name = fund["name"]
        point = results.get(code)
        if point:
            lines.extend([
                f"- **{name}**（`{code}`）",
                f"  - 单位净值：**{point.net_value:.4f}**",
                f"  - 累计净值：**{point.acc_value:.4f}**",
                f"  - 日涨跌幅：**{point.daily_change:+.2f}%**",
                f"  - 数据来源：{point.source}",
            ])
            if point.volume > 0:
                lines.append(f"  - 成交量：{point.volume:.0f}")
            # 对应指数提示：按名称匹配
            idx_name = _match_index_name(name, index_results)
            if idx_name:
                idx_code = None
                for cfg_idx in cfg.indices:
                    if cfg_idx["name"] == idx_name:
                        idx_code = cfg_idx["code"]
                        break
                if idx_code and idx_code in index_results:
                    idx_point = index_results[idx_code]
                    lines.append(f"  - 对应指数：{idx_name} {_format_change(idx_point.daily_change)}")
            lines.append("")

    # ── 组合风险分析 ─────────────────────────────────
    if risk_report:
        lines.extend([
            "---",
            "",
            "### 📊 组合风险分析",
            "",
            "| 指标 | 值 |",
            "|------|:---:|",
            f"| 组合今日收益率 | {_fmt_pct(risk_report['portfolio_return'])} |",
            f"| 累积收益率 | {_fmt_pct(risk_report['cumulative_return'])} |",
            f"| 最大回撤 | {_fmt_risk(risk_report['max_drawdown'])} |",
            f"| 日波动率 | {risk_report['volatility']:.2f}% |",
            f"| {risk_report['benchmark_name']} 同期收益 | {_fmt_pct(risk_report['benchmark_return'])} |",
            "",
        ])

        # 判读仓位建议（简易规则）
        dd = risk_report['max_drawdown']
        vol = risk_report['volatility']
        advice_parts = []
        if dd < -10:
            advice_parts.append("⚠️ 最大回撤超过 10%，建议审视组合风险敞口")
        elif dd < -5:
            advice_parts.append("🔸 最大回撤处于可控范围，继续观察")
        else:
            advice_parts.append("✅ 最大回撤在 5% 以内，风控良好")
        if vol > 2:
            advice_parts.append("📊 波动率偏高，组合可能偏向成长型")
        elif vol < 0.5:
            advice_parts.append("📊 波动率偏低，组合偏向稳健")
        else:
            advice_parts.append("📊 波动率适中")

        # 与基准对比
        port_ret = risk_report['cumulative_return']
        bench_ret = risk_report['benchmark_return']
        diff = port_ret - bench_ret
        if diff > 0:
            advice_parts.append(f"📈 组合跑赢 {risk_report['benchmark_name']} {diff:+.2f}%")
        elif diff < 0:
            advice_parts.append(f"📉 组合跑输 {risk_report['benchmark_name']} {diff:+.2f}%")
        else:
            advice_parts.append(f"📊 组合与 {risk_report['benchmark_name']} 表现持平")

        for line in advice_parts:
            lines.append(line)
        lines.append("")

    # ── AI 分析 ──────────────────────────────────────
    if ai_analysis:
        lines.extend([
            "---",
            "",
            "### 🧠 AI 分析",
            "",
            ai_analysis,
            "",
        ])

    # ── 错误信息 ─────────────────────────────────────
    if errors:
        lines.append(errors)
        lines.append("")

    # ── 页脚 ─────────────────────────────────────────
    lines.extend([
        "---",
        "",
        f"🔄 _由 ETF-Tracker 自动生成 · {now.strftime('%Y-%m-%d %H:%M')} CST_",
        "",
    ])

    return "\n".join(lines)


def _match_index_name(fund_name: str, index_results: dict) -> str:
    """根据基金名称匹配对应的指数名称（只匹配指数数据，不匹配商品）"""
    for code, point in index_results.items():
        if not point:
            continue
        if code.startswith("commodity:"):
            continue  # 跳过商品行情
        # 跳过 commodity 前缀（兼容旧数据中的其他格式）
        idx_name = point.name
        # 去掉前缀进行匹配
        kw = idx_name.replace("上证", "").replace("中证", "")
        if kw in fund_name:
            return idx_name  # 返回完整指数名称
    return ""


def generate_plaintext_report(report_md: str) -> str:
    """
    将 Markdown 日报转成纯文本（用于邮件纯文本 fallback）
    简单去除 markdown 格式
    """
    import re
    text = report_md
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)  # 去加粗
    text = re.sub(r"\|.*?\|", "", text, flags=re.MULTILINE)  # 去表格
    text = re.sub(r"^[-—]{3,}$", "", text, flags=re.MULTILINE)  # 去分隔线
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
