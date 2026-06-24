#!/usr/bin/env python3
"""
ETF Tracker — 主入口

每日定时运行：
  1. 读取配置
  2. 多源爬取基金/ETF数据
  3. 交叉验证
  4. 爬取大盘指数 + 商品行情（新增）
  5. （可选）AI 分析
  6. 生成日报（含指数和黄金板块）
  7. 发送 Outlook 邮件
"""

import sys
import os
from pathlib import Path

# 确保项目根在 sys.path 中（在 GitHub Actions 中也会自动处理）
_SRC = Path(__file__).resolve().parent
_PROJ = _SRC.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from src.config import Config
from src.fetchers.eastmoney_fund import EastMoneyFundFetcher
from src.fetchers.fallback import FallbackFetcher
from src.fetchers.index_fetcher import fetch_all_indices
from src.validator import Validator
from src.analyzer import analyze
from src.reporter import generate_report
from src.mailer import send_report
from src.storage import save_snapshot
from src.risk import get_risk_report


def main():
    print("=" * 50)
    print("📊 ETF Tracker — 开始运行")
    print("=" * 50)

    # 1. 加载配置
    cfg = Config()
    track_count = len(cfg.funds) + len(cfg.indices) + len(cfg.commodities)
    print(f"📋 配置加载完成 | 基金: {len(cfg.funds)} | 指数: {len(cfg.indices)} | 商品: {len(cfg.commodities)}")

    # 2. 初始化基金数据获取器（主 + 备）
    primary = EastMoneyFundFetcher()
    fallback = FallbackFetcher()
    fetchers = [primary, fallback]

    # 3. 交叉验证获取基金数据
    print("\n📡 正在获取基金净值数据...")
    validator = Validator(cfg, fetchers)
    results = validator.run(cfg.funds)

    if not results:
        print("\n⚠️ 所有基金数据获取均失败，继续获取指数和黄金...")
    else:
        print(f"\n✅ 成功获取 {len(results)}/{len(cfg.funds)} 只基金数据")

    # 4. 获取大盘指数 + 商品行情（新增）
    index_results = {}
    if cfg.indices or cfg.commodities:
        print("\n📊 正在获取大盘指数 & 黄金行情...")
        index_results = fetch_all_indices(cfg.indices, cfg.commodities)
        if index_results:
            print(f"\n✅ 成功获取 {len(index_results)} 项指数/商品数据")
        else:
            print("\n⚠️ 指数/商品数据全部获取失败")
    else:
        print("\n⏭ 未配置指数/商品跟踪（跳过）")

    # 5.5 保存历史数据（在 AI 分析之前，确保数据已入库）
    print("\n💾 保存历史数据...")
    save_snapshot(results, index_results)

    # 5.6 组合风险分析
    print("\n📊 组合风险分析...")
    risk_report = get_risk_report(cfg, results, days=30)
    if risk_report and risk_report.get("cumulative_return") != 0:
        print(f"  📈 组合今日收益率: {risk_report['portfolio_return']:+.2f}%")
        print(f"  📉 累积收益率: {risk_report['cumulative_return']:+.2f}%")
        print(f"  🔻 最大回撤: {risk_report['max_drawdown']:.2f}%")
    else:
        print("  ⏭ 历史数据不足（需至少2条记录），跳过风险分析")
        risk_report = None

    # 5. AI 分析（可选）- 传入风险数据供参考
    print("\n🧠 AI 分析...")
    ai_result = analyze(cfg, results, index_results, risk_report)
    if ai_result and ai_result.startswith("🤖"):
        print(f"  {ai_result}（未启用）")
    elif ai_result:
        print(f"  ✅ AI 分析完成")
    else:
        print("  ⏭ AI 分析未启用")

    # 6. 生成日报
    print("\n📝 生成日报...")
    errors = validator.get_report_errors()
    report = generate_report(cfg, results, index_results, ai_result, errors, risk_report)

    # 保存到 data/ 目录（留档）
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8)))
    date_str = now.strftime('%Y%m%d')
    report_path = _PROJ / "data" / f"report_{date_str}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    # 如果当天已有日报，更新时保留时间戳便于比对
    report_path.write_text(report, encoding="utf-8")
    print(f"  ✅ 日报已保存: {report_path}")

    # 打印预览
    print("\n" + "─" * 40)
    print("📰 日报预览（前 500 字）:")
    print(report[:500])
    print("..." if len(report) > 500 else "")
    print("─" * 40)

    # 7. 发送邮件
    print(f"\n📧 发送邮件至 {cfg.mail_to}...")
    try:
        send_report(cfg, report)
        print("\n🎉 全部完成！")
    except Exception as e:
        print(f"\n{e}")
        # 仍然保留本地日报
        print(f"📁 日报已保留在本地: {report_path}")
        sys.exit(1)


if __name__ == "__main__":
    main()
