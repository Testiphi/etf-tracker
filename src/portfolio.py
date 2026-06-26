"""
组合/仓位管理模块

功能：
  1. 从 SQLite 加载用户持仓记录（份数、成本、买入日期）
  2. 结合当日净值（从 results 字典获取）计算市值与盈亏
  3. 持仓与 settings.yaml 中 weight（投入金额）的双向同步
  4. 未来扩展：图片识别导入基金仓位（预留抽象接口）

数据流：
  settings.yaml (静态仓位) ─┬─→ portfolio.sync_from_config()
                            └─→ portfolio 可直接从 SQLite 读取/写入
"""

import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from .config import Config
from .fetchers.base import FundDataPoint

_DB_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_PATH = _DB_DIR / "portfolio.db"


# ═══════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════

@dataclass
class Holding:
    """一条持仓记录"""
    id: int = 0
    fund_code: str = ""
    fund_name: str = ""
    shares: float = 0.0          # 持有份数
    cost_basis: float = 0.0      # 成本/累计投入金额（元）
    purchase_date: str = ""      # 买入日期 YYYY-MM-DD
    note: str = ""               # 备注
    # 以下由 compute_current_value 填充（不持久化）
    current_nav: float = 0.0
    current_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0


@dataclass
class Portfolio:
    """整体组合"""
    holdings: list[Holding] = field(default_factory=list)
    total_cost: float = 0.0
    total_value: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0


# ═══════════════════════════════════════════════════════════════
# 数据库操作
# ═══════════════════════════════════════════════════════════════

def _ensure_db():
    """确保数据库目录和持仓表存在"""
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_holdings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            fund_code    TEXT NOT NULL,
            fund_name    TEXT DEFAULT '',
            shares       REAL DEFAULT 0,
            cost_basis   REAL DEFAULT 0,
            purchase_date TEXT DEFAULT '',
            note         TEXT DEFAULT '',
            created_at   TEXT DEFAULT (datetime('now','localtime')),
            updated_at   TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_config (
            fund_code    TEXT PRIMARY KEY,
            weight       REAL DEFAULT 0,      -- settings.yaml 中的 weight（元）
            shares       REAL DEFAULT 0,      -- settings.yaml 中的 shares（份数）
            auto_calc    INTEGER DEFAULT 0,    -- 是否自动从 shares × NAV 算 weight
            updated_at   TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.commit()
    conn.close()


def save_holding(h: Holding) -> int:
    """新增或更新一条持仓记录。返回 id。"""
    _ensure_db()
    conn = sqlite3.connect(str(_DB_PATH))
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    if h.id:
        conn.execute(
            """UPDATE portfolio_holdings
               SET fund_code=?, fund_name=?, shares=?, cost_basis=?,
                   purchase_date=?, note=?, updated_at=?
               WHERE id=?""",
            (h.fund_code, h.fund_name, h.shares, h.cost_basis,
             h.purchase_date, h.note, now, h.id),
        )
    else:
        cur = conn.execute(
            """INSERT INTO portfolio_holdings
               (fund_code, fund_name, shares, cost_basis, purchase_date, note, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (h.fund_code, h.fund_name, h.shares, h.cost_basis,
             h.purchase_date, h.note, now, now),
        )
        h.id = cur.lastrowid
    conn.commit()
    conn.close()
    return h.id


def delete_holding(holding_id: int):
    """删除一条持仓记录"""
    _ensure_db()
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("DELETE FROM portfolio_holdings WHERE id=?", (holding_id,))
    conn.commit()
    conn.close()


def load_holdings() -> list[Holding]:
    """从数据库加载所有持仓记录"""
    _ensure_db()
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM portfolio_holdings ORDER BY fund_code, purchase_date"
    ).fetchall()
    conn.close()
    holdings = []
    for r in rows:
        holdings.append(Holding(
            id=r["id"],
            fund_code=r["fund_code"],
            fund_name=r["fund_name"] or "",
            shares=r["shares"] or 0.0,
            cost_basis=r["cost_basis"] or 0.0,
            purchase_date=r["purchase_date"] or "",
            note=r["note"] or "",
        ))
    return holdings


# ═══════════════════════════════════════════════════════════════
# 配置同步
# ═══════════════════════════════════════════════════════════════

def sync_from_config(cfg: Config):
    """
    将 settings.yaml 中的 funds 同步到 portfolio_config 表。
    settings.yaml 是权威来源，每次运行自动同步。

    新增字段：
      - weight: 投入金额（元），已在用
      - shares: 基金份数（可选），未来用户可直接填份数
    """
    _ensure_db()
    conn = sqlite3.connect(str(_DB_PATH))
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    for fund in cfg.funds:
        auto = 1 if fund.get("shares", 0) > 0 and fund.get("weight", 0) == 0 else 0
        conn.execute(
            """INSERT OR REPLACE INTO portfolio_config
               (fund_code, weight, shares, auto_calc, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (fund["code"], fund.get("weight", 0), fund.get("shares", 0), auto, now),
        )
    conn.commit()
    conn.close()
    print(f"  📋 已同步 {len(cfg.funds)} 条仓位配置")


def get_config_weight(fund_code: str) -> float:
    """从 portfolio_config 获取某基金的 weight（元）"""
    _ensure_db()
    conn = sqlite3.connect(str(_DB_PATH))
    row = conn.execute(
        "SELECT weight FROM portfolio_config WHERE fund_code=?", (fund_code,)
    ).fetchone()
    conn.close()
    return row[0] if row else 0.0


# ═══════════════════════════════════════════════════════════════
# 市值 & 盈亏计算
# ═══════════════════════════════════════════════════════════════

def compute_portfolio(
    cfg: Config,
    results: dict[str, FundDataPoint],
) -> Portfolio:
    """
    计算完整组合市值与盈亏

    持仓来源（优先级从高到低）：
      1. portfolio_holdings 表（用户手动录入的份数+成本）
      2. settings.yaml 中的 weight（投入金额）—— 没有明细时退化为单条总持仓

    计算规则：
      - 当前市值 = 份数 × 当日单位净值
      - 盈亏 = 当前市值 - 成本
      - 成本从 cost_basis（录入）或 weight（yaml）获取
      - 如果只有 weight（金额）没有份数，从 weight/NAV ≈ 份数
    """
    _ensure_db()
    db_holdings = load_holdings()
    results_nav: dict[str, float] = {
        code: pt.net_value for code, pt in results.items() if pt and pt.net_value
    }

    portfolio = Portfolio()
    seen_codes: set[str] = set()

    # ── 步骤 1: 从 portfolio_holdings 表加载明细 ──────────
    for h in db_holdings:
        if h.shares <= 0 and h.cost_basis <= 0:
            continue
        nav = results_nav.get(h.fund_code)
        if nav:
            h.current_nav = nav
            h.current_value = round(h.shares * nav, 2)
        else:
            h.current_nav = 0
            h.current_value = 0.0

        if h.cost_basis > 0:
            h.pnl = round(h.current_value - h.cost_basis, 2)
            h.pnl_pct = round((h.pnl / h.cost_basis) * 100, 2) if h.cost_basis > 0 else 0.0
        else:
            h.pnl = 0.0
            h.pnl_pct = 0.0

        portfolio.holdings.append(h)
        seen_codes.add(h.fund_code)
        portfolio.total_cost += h.cost_basis
        portfolio.total_value += h.current_value

    # ── 步骤 2: 从 config 中补充没有明细持仓的基金 ────────
    for fund in cfg.funds:
        code = fund["code"]
        if code in seen_codes:
            continue

        name = fund["name"]
        weight = fund.get("weight", 0) or 0
        cfg_shares = fund.get("shares", 0) or 0
        nav = results_nav.get(code)

        if weight <= 0 and cfg_shares <= 0:
            continue

        h = Holding(fund_code=code, fund_name=name)

        if cfg_shares > 0:
            h.shares = cfg_shares
            h.cost_basis = weight if weight > 0 else 0.0
        else:
            # 只有 weight（金额），没有份数
            h.cost_basis = weight
            if nav and nav > 0:
                h.shares = round(weight / nav, 4)
                h.current_nav = nav
                h.current_value = round(h.shares * nav, 2)
            else:
                h.shares = 0.0
                h.current_value = 0.0

        h.note = "来自 settings.yaml"

        if h.cost_basis > 0:
            h.pnl = round(h.current_value - h.cost_basis, 2)
            h.pnl_pct = round((h.pnl / h.cost_basis) * 100, 2) if h.cost_basis > 0 else 0.0

        portfolio.holdings.append(h)
        portfolio.total_cost += h.cost_basis
        portfolio.total_value += h.current_value

    # ── 汇总 ─────────────────────────────────────────────
    portfolio.total_pnl = round(portfolio.total_value - portfolio.total_cost, 2)
    portfolio.total_pnl_pct = round(
        (portfolio.total_pnl / portfolio.total_cost) * 100, 2
    ) if portfolio.total_cost > 0 else 0.0

    return portfolio


# ═══════════════════════════════════════════════════════════════
# 未来扩展：图片识别导入（预留接口）
# ═══════════════════════════════════════════════════════════════

class FundImageParser:
    """
    图片识别基金仓位 —— 抽象基类

    未来实现：
      - OCR 识别基金名称/代码
      - 识别持有金额/份数
      - 返回 Holding 列表

    目前为占位桩，供后续开发。
    """

    def parse(self, image_path: str) -> list[Holding]:
        """
        从截图/图片中解析出持仓列表

        Args:
            image_path: 图片文件路径（本地路径或 URL）

        Returns:
            list[Holding] — 识别到的持仓记录

        Raises:
            NotImplementedError: 尚未实现
        """
        raise NotImplementedError(
            "图片识别导入功能尚未实现。"
            "\n计划支持："
            "\n  - 支付宝/天天基金持仓截图"
            "\n  - 券商 APP 持仓截图"
            "\n  - 基金公司 APP 截图"
            "\n请关注后续开发。"
        )

    @staticmethod
    def supported_formats() -> list[str]:
        """返回支持的图片格式"""
        return ["待实现"]
