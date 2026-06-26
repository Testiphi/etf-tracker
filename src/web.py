"""
ETF Tracker — Web 前端（FastAPI 后端）

缓存策略：
  GET /api/*  → 从 data/cache_portfolio.json 读取（秒级响应）
  POST /api/refresh → 触发全量数据抓取并更新缓存

运行：uvicorn src.web:app --reload --port 8080
"""

import sys, os, json, asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 项目根路径
_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from src.config import Config
from src.storage import get_history
from src.risk import _get_weights, _portfolio_net_value_series

CACHE_PATH = _PROJ / "data" / "cache_portfolio.json"
cfg = Config()

app = FastAPI(title="ETF Tracker", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def startup():
    """启动时自动尝试刷新缓存（后台，不阻塞启动）"""
    cached = _load_cache()
    if cached:
        print(f"[web] 缓存已就绪: {cached['date']}")
    else:
        print("[web] 无缓存，启动后台刷新...")
        asyncio.create_task(_background_refresh())

# ── 工具函数 ────────────────────────────────────────────────

async def _background_refresh():
    """后台刷新缓存（首次启动或缓存过期）"""
    try:
        data = await _build_portfolio_data()
        _save_cache(data)
        print(f"[web] 后台刷新完成: {data['date']} ({data['fetched_count']}/{data['funds_count']})")
    except Exception as e:
        print(f"[web] 后台刷新失败: {e}")


def _load_cache():
    """加载缓存，不存在返回 None"""
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None

def _save_cache(data: dict):
    """写入缓存"""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

async def _build_portfolio_data():
    """全量抓取 + 组合计算（耗时 30-60s）"""
    from src.fetchers.eastmoney_fund import EastMoneyFundFetcher
    from src.fetchers.index_fetcher import fetch_all_indices
    from src.portfolio import sync_from_config, sync_plans_from_config, compute_portfolio, load_plans

    fetcher = EastMoneyFundFetcher(timeout=15)
    print("[web] 获取基金数据...")
    results = {}
    for f in cfg.funds:
        try:
            pt = fetcher.fetch(f["code"], f["name"], f.get("type", "fund"))
            if pt:
                results[f["code"]] = pt
        except Exception:
            pass

    print(f"[web] 基金: {len(results)}/{len(cfg.funds)}")

    index_results = {}
    if cfg.indices or cfg.commodities:
        print("[web] 获取指数&黄金...")
        index_results = fetch_all_indices(cfg.indices, cfg.commodities)

    sync_from_config(cfg)
    sync_plans_from_config(cfg)
    portfolio = compute_portfolio(cfg, results)

    now = datetime.now(timezone(timedelta(hours=8)))
    date_str = now.strftime("%Y-%m-%d %H:%M")

    holdings_data = []
    for h in portfolio.holdings:
        holdings_data.append({
            "id": h.id,
            "fund_code": h.fund_code,
            "fund_name": h.fund_name or h.fund_code,
            "shares": round(h.shares, 4),
            "cost_basis": round(h.cost_basis, 2),
            "current_nav": round(h.current_nav, 4),
            "current_value": round(h.current_value, 2),
            "pnl": round(h.pnl, 2),
            "pnl_pct": round(h.pnl_pct, 2),
            "note": h.note,
        })

    plans = load_plans()
    plans_data = [
        {
            "id": p.id,
            "fund_code": p.fund_code,
            "fund_name": p.fund_name or p.fund_code,
            "amount": p.amount,
            "period": p.period,
            "note": p.note,
        }
        for p in plans
    ]

    data = {
        "date": date_str,
        "total_cost": round(portfolio.total_cost, 2),
        "total_value": round(portfolio.total_value, 2),
        "total_pnl": round(portfolio.total_pnl, 2),
        "total_pnl_pct": round(portfolio.total_pnl_pct, 2),
        "holdings": holdings_data,
        "plans": plans_data,
        "funds_count": len(cfg.funds),
        "fetched_count": len(results),
    }
    return data


# ── 静态仪表盘 HTML ─────────────────────────────────────────

_HTML_PATH = _PROJ / "src" / "web_dashboard.html"

@app.get("/", response_class=HTMLResponse)
async def index():
    if _HTML_PATH.exists():
        return _HTML_PATH.read_text(encoding="utf-8")
    return "<h1>ETF Tracker</h1><p>Dashboard not found</p>"


# ── API ─────────────────────────────────────────────────────

@app.get("/api/portfolio")
async def get_portfolio():
    """返回缓存的组合数据（立即响应）"""
    cached = _load_cache()
    if cached:
        return cached
    return {"status": "waiting", "date": None, "message": "首次刷新中，请稍后刷新页面"}


@app.post("/api/refresh")
async def refresh():
    """手动触发全量数据刷新"""
    data = await _build_portfolio_data()
    _save_cache(data)
    return JSONResponse({"status": "ok", "date": data["date"],
                         "fetched": f"{data['fetched_count']}/{data['funds_count']}"})


@app.get("/api/history/portfolio")
async def get_portfolio_history(days: int = 60):
    """组合加权净值曲线"""
    codes = [f["code"] for f in cfg.funds]
    history = get_history(codes, days=days)
    nav_series = _portfolio_net_value_series(cfg, history)

    if not nav_series:
        return {"dates": [], "values": []}

    weights = _get_weights(cfg)
    data_map = {}
    for code in codes:
        records = history.get(code, [])
        data_map[code] = {r["date"]: r["net_value"] for r in records}

    all_dates = set()
    for code in codes:
        all_dates.update(data_map[code].keys())
    common_dates = sorted(
        d for d in all_dates
        if all((data_map[c].get(d) or 0) > 0 for c in codes)
    )

    return {
        "dates": common_dates[:len(nav_series)],
        "values": [round(v * 100, 2) for v in nav_series],
    }


# ── API: 定投计划 ───────────────────────────────────────

@app.get("/api/plans")
async def list_plans():
    from src.portfolio import load_plans
    plans = load_plans()
    return [
        {
            "id": p.id,
            "fund_code": p.fund_code,
            "fund_name": p.fund_name or p.fund_code,
            "amount": p.amount,
            "period": p.period,
            "note": p.note,
        }
        for p in plans
    ]


class PlanInput(BaseModel):
    fund_code: str
    fund_name: str = ""
    amount: float = 0.0
    period: str = "daily"
    note: str = ""


@app.post("/api/plans")
async def create_plan(p: PlanInput):
    from src.portfolio import Plan as PlanModel, save_plan
    plan = PlanModel(
        fund_code=p.fund_code,
        fund_name=p.fund_name,
        amount=p.amount,
        period=p.period if p.period else "daily",
        note=p.note,
    )
    save_plan(plan)
    return {"status": "ok", "id": plan.id}


@app.delete("/api/plans/{plan_id}")
async def remove_plan(plan_id: int):
    from src.portfolio import delete_plan
    delete_plan(plan_id)
    return {"status": "ok"}


@app.get("/api/funds")
async def list_funds():
    """配置中的基金列表"""
    return [{"code": f["code"], "name": f["name"],
             "weight": f.get("weight", 0)} for f in cfg.funds]


# ── 运行 ────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
