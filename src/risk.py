"""
组合风险分析模块

基于历史净值数据和当前持仓权重，计算：
  - 组合加权日收益率
  - 组合累积收益率
  - 最大回撤 (Max Drawdown)
  - 基准对比
  - 波动率 / 夏普比率（数据充足时）
"""

import math
from typing import Optional

from .config import Config
from .storage import get_history


# 组合中各基金的权重配比（从 config 的 weight 字段读取）
def _get_weights(cfg: Config) -> dict[str, float]:
    """计算组合中每个基金的标准化权重"""
    total = sum(f.get("weight", 1) for f in cfg.funds)
    return {f["code"]: f.get("weight", 1) / total for f in cfg.funds}


def portfolio_daily_return(cfg: Config, results: dict) -> Optional[float]:
    """
    计算组合加权日收益率

    权重来自 config 中每个基金配置的 `weight` 字段。
    如某基金当日无数据，该基金份额视为零收益参与加权。
    """
    weights = _get_weights(cfg)
    weighted_sum = 0.0
    for code, weight in weights.items():
        point = results.get(code)
        if point and point.daily_change is not None:
            weighted_sum += weight * point.daily_change
    return round(weighted_sum, 2)


def _safe_float(val) -> float:
    """安全转为 float，None/NaN 返回 0.0"""
    if val is None:
        return 0.0
    try:
        v = float(val)
        if math.isnan(v) or math.isinf(v):
            return 0.0
        return v
    except (ValueError, TypeError):
        return 0.0


def max_drawdown(prices: list[float]) -> float:
    """
    计算最大回撤（百分比）

    给定一组价格序列（从旧到新），找出从某个峰值回撤到谷底的最大跌幅。
    返回负百分比（如 -8.5 表示最大回撤 8.5%）。
    """
    if len(prices) < 2:
        return 0.0

    peak = prices[0]
    max_dd = 0.0
    for p in prices:
        if p > peak:
            peak = p
        dd = (p - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd
    return round(max_dd, 2)


def cumulative_return(prices: list[float]) -> float:
    """累积收益率（百分比）"""
    if len(prices) < 2 or prices[0] == 0:
        return 0.0
    return round((prices[-1] - prices[0]) / prices[0] * 100, 2)


def _portfolio_net_value_series(
    cfg: Config,
    history: dict[str, list[dict]],
) -> list[float]:
    """
    通过加权各基金净值生成组合净值序列

    组合净值 = Σ(各基金权重 × 当日单位净值)
    以第一条数据为基准归一化到 1.0，后续按比例缩放。
    只取所有基金都有数据的日期。

    Returns:
        归一化后的组合净值列表（从旧到新）
    """
    weights = _get_weights(cfg)
    codes = list(weights.keys())

    # 为每个 code 建立 date → net_value 映射
    data_map: dict[str, dict[str, float]] = {}
    for code in codes:
        records = history.get(code, [])
        data_map[code] = {r["date"]: _safe_float(r["net_value"]) for r in records}

    # 找出所有基金都有数据的日期
    all_dates: set[str] = set()
    for code in codes:
        all_dates.update(data_map[code].keys())
    common_dates = sorted(
        d for d in all_dates
        if all(data_map[c].get(d, 0) > 0 for c in codes)
    )

    if len(common_dates) < 2:
        return []

    # 计算每个日期的组合净值
    raw_values = []
    for date in common_dates:
        value = sum(weights[c] * data_map[c][date] for c in codes)
        raw_values.append(value)

    # 归一化到基准 = 1.0（第一天为 1.0）
    base = raw_values[0]
    if base <= 0:
        return []
    return [v / base for v in raw_values]


def _benchmark_net_value_series(
    cfg: Config,
    history: dict[str, list[dict]],
    benchmark_code: str = "^GSPC",
) -> list[float]:
    """
    生成基准（如标普500）的归一化净值序列
    用于与组合收益率对比
    """
    records = history.get(benchmark_code, [])
    if len(records) < 2:
        return []

    values = [_safe_float(r["net_value"]) for r in records]
    base = values[0]
    if base <= 0:
        return []
    return [v / base for v in values]


def get_risk_report(
    cfg: Config,
    results: dict,
    days: int = 30,
) -> dict:
    """
    生成组合风险分析报告

    Returns:
        {
            "portfolio_return": float,        # 组合加权日收益率 (%)
            "cumulative_return": float,       # 组合累积收益率 (%)
            "max_drawdown": float,            # 最大回撤 (%)
            "benchmark_return": float,        # 同期基准累积收益率 (%)
            "benchmark_code": str,            # 用于对比的基准代码
            "volatility": float,              # 日波动率（最近 N 天）
        }
    """
    weights = _get_weights(cfg)
    codes = list(weights.keys())

    # 从历史数据库获取近期数据
    history = get_history(codes, days=days)

    # 组合加权日收益率
    port_return = portfolio_daily_return(cfg, results)

    # 组合净值序列（用于计算累积收益率和最大回撤）
    nav_series = _portfolio_net_value_series(cfg, history)

    cum_return = 0.0
    max_dd = 0.0

    if len(nav_series) >= 2:
        cum_return = cumulative_return(nav_series)
        max_dd = max_drawdown(nav_series)

    # 基准对比（标普500）
    benchmark_code = "^GSPC"
    bench_history = get_history([benchmark_code], days=days)
    bench_series = _benchmark_net_value_series(cfg, bench_history)
    bench_return = cumulative_return(bench_series) if len(bench_series) >= 2 else 0.0

    # 波动率（最近 N 天日收益率的标准差）
    volatility = 0.0
    if len(nav_series) >= 3:
        daily_returns = []
        for i in range(1, len(nav_series)):
            dr = (nav_series[i] - nav_series[i-1]) / nav_series[i-1] * 100
            daily_returns.append(dr)
        if daily_returns:
            mean = sum(daily_returns) / len(daily_returns)
            variance = sum((r - mean) ** 2 for r in daily_returns) / len(daily_returns)
            volatility = round(math.sqrt(variance), 2)

    return {
        "portfolio_return": port_return or 0.0,
        "cumulative_return": cum_return,
        "max_drawdown": max_dd,
        "benchmark_return": bench_return,
        "benchmark_code": benchmark_code,
        "benchmark_name": "标普500",
        "volatility": volatility,
    }
