"""
历史净值存储模块（SQLite）

每日运行后将当日基金/指数/商品数据存入 SQLite 数据库，
供后续风险分析（最大回撤、波动率等）使用。
"""

import sqlite3
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

from .fetchers.base import FundDataPoint

# 数据库文件路径（项目根目录下的 data/portfolio.db）
_DB_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_PATH = _DB_DIR / "portfolio.db"


def _ensure_db():
    """确保数据库目录和表存在"""
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nav_history (
            code       TEXT NOT NULL,
            date       TEXT NOT NULL,
            net_value  REAL,
            acc_value  REAL,
            daily_change REAL,
            name       TEXT,
            source     TEXT,
            category   TEXT DEFAULT 'fund',   -- fund / index / commodity
            PRIMARY KEY (code, date)
        )
    """)
    # 索引：按日期快速查询
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nav_date ON nav_history(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nav_code ON nav_history(code)")
    conn.commit()
    conn.close()


def save_snapshot(
    results: dict[str, FundDataPoint],
    index_results: dict[str, FundDataPoint],
    today: Optional[str] = None,
):
    """
    将当日数据写入历史数据库

    Args:
        results: code → FundDataPoint（基金）
        index_results: code → FundDataPoint（指数 + commodity: 前缀商品）
        today: 日期字符串 YYYY-MM-DD，默认当天北京时间
    """
    if today is None:
        now = datetime.now(timezone(timedelta(hours=8)))
        today = now.strftime("%Y-%m-%d")

    _ensure_db()
    conn = sqlite3.connect(str(_DB_PATH))

    rows = 0
    for code, pt in results.items():
        if pt is None:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO nav_history
               (code, date, net_value, acc_value, daily_change, name, source, category)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'fund')""",
            (code, today, pt.net_value, pt.acc_value, pt.daily_change, pt.name, pt.source),
        )
        rows += 1

    for code, pt in index_results.items():
        if pt is None:
            continue
        category = "commodity" if code.startswith("commodity:") else "index"
        conn.execute(
            """INSERT OR REPLACE INTO nav_history
               (code, date, net_value, acc_value, daily_change, name, source, category)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, today, pt.net_value, pt.acc_value, pt.daily_change, pt.name, pt.source, category),
        )
        rows += 1

    conn.commit()
    conn.close()
    print(f"  💾 已保存 {rows} 条记录到历史数据库")


def get_history(
    codes: list[str],
    days: int = 30,
) -> dict[str, list[dict]]:
    """
    获取多个代码的最近 N 天历史数据

    Returns:
        { code: [{date, net_value, daily_change}, ...], ... }
        按日期升序排列
    """
    _ensure_db()
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row

    result = {}
    for code in codes:
        rows = conn.execute(
            """SELECT date, net_value, daily_change, name
               FROM nav_history
               WHERE code = ?
               ORDER BY date DESC
               LIMIT ?""",
            (code, days),
        ).fetchall()
        result[code] = [
            {"date": r["date"], "net_value": r["net_value"],
             "daily_change": r["daily_change"], "name": r["name"]}
            for r in reversed(rows)  # 升序
        ]

    conn.close()
    return result


def get_all_codes() -> list[str]:
    """获取数据库中有记录的所有 code"""
    _ensure_db()
    conn = sqlite3.connect(str(_DB_PATH))
    rows = conn.execute("SELECT DISTINCT code FROM nav_history").fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_latest_date() -> Optional[str]:
    """获取数据库中最新记录的日期"""
    _ensure_db()
    conn = sqlite3.connect(str(_DB_PATH))
    row = conn.execute("SELECT MAX(date) FROM nav_history").fetchone()
    conn.close()
    return row[0] if row else None
