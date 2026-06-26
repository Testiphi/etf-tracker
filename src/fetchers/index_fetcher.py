"""
大盘指数 / 商品行情获取器

支持：
  - 中国A股指数（东方财富行情API）：科创50、中证A500 等
  - 美股指数（雅虎财经API）：标普500、纳斯达克100 等
  - 黄金现货（东方财富行情API）：AU9999

无需额外依赖，只使用 requests + PyYAML（已存在）。
"""

import json
import re
import requests
from datetime import datetime, timezone, timedelta

from .base import FundDataPoint

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}

# 东方财富行情 API
EM_ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"  # 指数列表（正确）
EM_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"     # 单只行情（备用）

# 雅虎财经 API（美股指数 + 黄金期货备用）
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_CHART_URL_V2 = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"


class IndexFetcher:
    """大盘指数/商品行情获取器"""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._yahoo_session: requests.Session | None = None

    # ── 雅虎财经辅助（Cookie + Crumb 绕过 403）────────────────

    def _get_yahoo_session(self) -> requests.Session | None:
        """
        初始化带 Cookie 和 Crumb 的雅虎财经会话。
        返回已配置好 crumb 参数的 session（附加 .crumb 属性）。
        """
        if self._yahoo_session is not None:
            return self._yahoo_session

        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/json,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })

        try:
            # 第一步：访问首页拿 Cookie（设置 cookies 容器）
            session.get("https://fc.yahoo.com/", timeout=self.timeout)

            # 第二步：获取 crumb
            crumb_resp = session.get(
                "https://query2.finance.yahoo.com/v1/test/getcrumb",
                timeout=self.timeout,
            )
            if crumb_resp.status_code == 200:
                crumb = crumb_resp.text.strip()
                session.crumb = crumb  # type: ignore
                print(f"    🍪 雅虎财经 crumb 获取成功")
            else:
                # crumb 获取失败，无 crumb 继续尝试
                session.crumb = ""  # type: ignore
                print(f"    ⚠ 雅虎财经 crumb 获取失败 ({crumb_resp.status_code})")
        except Exception as e:
            session.crumb = ""  # type: ignore
            print(f"    ⚠ 雅虎财经会话初始化失败: {e}")

        self._yahoo_session = session
        return session

    def _yahoo_fetch(self, symbol: str) -> dict | None:
        """
        通用雅虎财经数据获取

        Args:
            symbol: 股票/指数代码（如 ^GSPC, GC=F）

        Returns:
            解析后的 response dict，或 None
        """
        session = self._get_yahoo_session()
        encoded = symbol.replace("^", "%5E")
        crumb = getattr(session, "crumb", "")

        # 先尝试 query2，不行再 fallback 到 query1
        urls = [
            YAHOO_CHART_URL_V2.format(symbol=encoded),
            YAHOO_CHART_URL.format(symbol=encoded),
        ]

        for url in urls:
            try:
                params = {"interval": "1d", "range": "5d"}
                if crumb:
                    params["crumb"] = crumb

                resp = session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 403:
                    continue  # 尝试下一个 URL
                else:
                    return None
            except Exception:
                continue

        # 都失败：尝试不带 session 的裸请求 + 备用 headers（GitHub Actions 场景）
        try:
            resp = requests.get(
                YAHOO_CHART_URL.format(symbol=encoded),
                params={"interval": "1d", "range": "5d"},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://finance.yahoo.com/",
                },
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass

        return None

    def fetch(self, code: str, name: str, market: str) -> FundDataPoint | None:
        """根据 market 类型路由到对应获取方法"""
        if market == "cn_index":
            return self._fetch_cn_index(code, name)
        elif market == "us_index":
            return self._fetch_us_index(code, name)
        elif market == "cn_gold":
            return self._fetch_cn_gold(code, name)
        else:
            print(f"  ⚠ 未知市场类型: {market} (code={code})")
            return None

    # ── 中国A股指数 ──────────────────────────────────

    def _fetch_cn_index(self, code: str, name: str) -> FundDataPoint | None:
        """通过东方财富 ulist API 获取A股指数数据（修正版）

        使用 ulist.np/get 端点 + 正确的字段映射：
          f2 = 最新价/指数值
          f3 = 涨跌幅(%)
          f5 = 成交量(手)
          f6 = 成交额(元)
          f12 = 代码
          f14 = 名称
        """
        secids = f"1.{code}"  # A股指数统一使用 1. 前缀

        params = {
            "fltt": "2",
            "fields": "f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18",
            "secids": secids,
        }

        try:
            resp = requests.get(
                EM_ULIST_URL,
                params=params,
                headers=HEADERS,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            raw = resp.json()
            diff = raw.get("data", {}).get("diff", [])
            if not diff:
                return None

            item = diff[0]
            price = float(item.get("f2", 0))
            change_pct = float(item.get("f3", 0))
            turnover = float(item.get("f6", 0))   # 成交额(元)，对指数更有意义

            if price <= 0:
                return None

            now = datetime.now(timezone(timedelta(hours=8)))
            return FundDataPoint(
                code=code,
                name=name,
                date=now.strftime("%Y-%m-%d"),
                net_value=round(price, 2),
                acc_value=0,
                daily_change=round(change_pct, 2),
                volume=round(turnover, 0),  # 用成交额替代成交量
                source="eastmoney-index-hq",
            )
        except Exception as e:
            print(f"  ⚠ 东方财富指数行情失败 [{name}]: {e}")
            return None

    # ── 美股指数 ──────────────────────────────────

    # 腾讯财经 US 代码映射
    _TENCENT_US_MAP = {
        "^GSPC": "usINX",   # 标普500
        "^NDX": "usNDX",    # 纳斯达克100
        "^IXIC": "usIXIC",  # 纳斯达克综合（备用）
        "^DJI": "usDJI",    # 道琼斯（备用）
    }

    def _fetch_us_index(self, code: str, name: str) -> FundDataPoint | None:
        """获取美股指数数据（多源：雅虎→腾讯）"""
        # 方法一：雅虎财经（从 GitHub Actions 可用）
        point = self._try_yahoo_us(code, name)
        if point:
            return point

        # 方法二：腾讯财经（从中国网络可用）
        point = self._try_tencent_us(code, name)
        if point:
            return point

        print(f"  ⚠ 所有美股数据源均失败 [{name}]")
        return None

    def _try_yahoo_us(self, code: str, name: str) -> FundDataPoint | None:
        """通过雅虎财经 API 获取美股指数数据"""
        try:
            resp_json = self._yahoo_fetch(code)
            if not resp_json:
                return None

            result = resp_json.get("chart", {}).get("result", [])
            if not result:
                return None

            meta = result[0].get("meta", {})
            timestamps = result[0].get("timestamp", [])
            indicators = result[0].get("indicators", {})
            quotes = indicators.get("quote", [{}])[0] if indicators.get("quote") else {}
            closes = quotes.get("close", []) or []
            volumes = quotes.get("volume", []) or []

            if not closes:
                return None

            last_complete_idx = len(closes) - 1
            if timestamps:
                now_ny = datetime.now(timezone.utc) - timedelta(hours=4)
                today_ny = now_ny.date()
                last_ts = datetime.fromtimestamp(timestamps[-1], tz=timezone.utc)
                last_day = (last_ts - timedelta(hours=4)).date()
                if last_day == today_ny and len(closes) > 1 and closes[-1] is None:
                    last_complete_idx = -2

            closest_idx = last_complete_idx
            while closest_idx >= 0 and closes[closest_idx] is None:
                closest_idx -= 1
            if closest_idx < 0:
                return None

            close_price = float(closes[closest_idx])
            prev_close = float(meta.get("chartPreviousClose", close_price) or close_price)
            change_pct = ((close_price - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
            volume = float(volumes[closest_idx]) if volumes and closest_idx < len(volumes) and volumes[closest_idx] else 0.0

            date_str = ""
            if timestamps and closest_idx < len(timestamps) and timestamps[closest_idx]:
                dt = datetime.fromtimestamp(timestamps[closest_idx], tz=timezone.utc)
                date_str = dt.strftime("%Y-%m-%d")

            return FundDataPoint(
                code=code,
                name=name,
                date=date_str,
                net_value=round(close_price, 2),
                acc_value=0,
                daily_change=round(change_pct, 2),
                volume=round(volume, 0),
                source="yahoo-finance",
            )
        except Exception as e:
            print(f"  ⚠ 雅虎财经失败 [{name}]: {e}")
            return None

    def _try_tencent_us(self, code: str, name: str) -> FundDataPoint | None:
        """
        通过腾讯财经 API 获取美股指数
        从国内网络可用，无需翻墙。
        """
        tc_code = self._TENCENT_US_MAP.get(code)
        if not tc_code:
            return None

        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tc_code},day,,,320,qfq"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()

            # 从 day 序列取最后一条完整数据
            day_data = data.get("data", {}).get(tc_code, {}).get("day", [])
            if not day_data:
                return None

            last = day_data[-1]
            # day 格式: [date, close, open, high, low, volume]
            if len(last) < 6:
                return None

            date_str = last[0]
            close_price = float(last[1])
            open_price = float(last[2])
            change_pct = round((close_price - open_price) / open_price * 100, 2) if open_price > 0 else 0.0
            volume = float(last[5]) if last[5] else 0.0

            return FundDataPoint(
                code=code,
                name=name,
                date=date_str,
                net_value=round(close_price, 2),
                acc_value=0,
                daily_change=change_pct,
                volume=round(volume, 0),
                source="tencent-finance",
            )
        except Exception as e:
            print(f"  ⚠ 腾讯财经失败 [{name}]: {e}")
            return None

    # ── 黄金现货 ──────────────────────────────────

    def _fetch_cn_gold(self, code: str, name: str) -> FundDataPoint | None:
        """获取黄金行情

        策略：
          1. 东方财富行情 API（AU9999）—— 从中国网络可用
          2. 雅虎财经黄金期货（GC=F）—— 从 GitHub Actions（海外）可用
        """
        # 方法一：东方财富
        point = self._try_eastmoney_gold(code, name)
        if point:
            return point

        # 方法二：雅虎财经黄金期货
        point = self._try_yahoo_gold(code, name)
        if point:
            return point

        print(f"  ⚠ 所有黄金数据源均失败 [{name}]")
        return None

    def _try_eastmoney_gold(self, code: str, name: str) -> FundDataPoint | None:
        """通过东方财富行情 API 获取黄金（AU9999）行情"""
        secid = f"1.{code}"

        params = {
            "secid": secid,
            "fields": "f43,f44,f45,f46,f47,f48,f50,f57,f58,f169,f170,f171,f172",
        }

        try:
            resp = requests.get(
                EM_QUOTE_URL,
                params=params,
                headers=HEADERS,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            if not data or not data.get("f43"):
                return None

            price = float(data.get("f43", 0))
            change_pct = float(data.get("f48", 0)) if data.get("f48") else 0.0
            volume = float(data.get("f169", 0)) if data.get("f169") else 0.0

            now = datetime.now(timezone(timedelta(hours=8)))
            return FundDataPoint(
                code=code,
                name=name,
                date=now.strftime("%Y-%m-%d"),
                net_value=round(price, 2),
                acc_value=0,
                daily_change=round(change_pct, 2),
                volume=round(volume, 0),
                source="eastmoney-gold-hq",
            )
        except Exception as e:
            print(f"  ⚠ 东方财富黄金行情失败 [{name}]: {e}")
            return None

    def _try_yahoo_gold(self, code: str, name: str) -> FundDataPoint | None:
        """通过雅虎财经获取黄金期货（GC=F）作为备用"""
        try:
            resp_json = self._yahoo_fetch("GC=F")
            if not resp_json:
                return None
            result = resp_json.get("chart", {}).get("result", [])
            if not result:
                return None

            meta = result[0].get("meta", {})
            timestamps = result[0].get("timestamp", [])
            indicators = result[0].get("indicators", {})
            quotes = indicators.get("quote", [{}])[0] if indicators.get("quote") else {}
            closes = quotes.get("close", []) or []
            volumes = quotes.get("volume", []) or []

            if not closes:
                return None

            closest_idx = len(closes) - 1
            while closest_idx >= 0 and closes[closest_idx] is None:
                closest_idx -= 1
            if closest_idx < 0:
                return None

            close_price = float(closes[closest_idx])
            prev_close = float(meta.get("chartPreviousClose", close_price) or close_price)
            change_pct = ((close_price - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
            volume = float(volumes[closest_idx]) if volumes and closest_idx < len(volumes) and volumes[closest_idx] else 0.0

            date_str = ""
            if timestamps and closest_idx < len(timestamps) and timestamps[closest_idx]:
                dt = datetime.fromtimestamp(timestamps[closest_idx], tz=timezone.utc)
                date_str = dt.strftime("%Y-%m-%d")

            return FundDataPoint(
                code=code,
                name=f"{name}(GC=F)",
                date=date_str,
                net_value=round(close_price, 2),
                acc_value=0,
                daily_change=round(change_pct, 2),
                volume=round(volume, 0),
                source="yahoo-gold-futures",
            )
        except Exception as e:
            print(f"  ⚠ 雅虎黄金期货失败: {e}")
            return None


def fetch_all_indices(
    indices_cfg: list[dict],
    commodities_cfg: list[dict],
    timeout: int = 15,
) -> dict[str, FundDataPoint]:
    """
    批量获取所有指数和商品行情

    Returns:
        {code: FundDataPoint, ...} 映射
    """
    fetcher = IndexFetcher(timeout=timeout)
    results: dict[str, FundDataPoint] = {}

    # 获取指数
    for item in indices_cfg:
        code = item["code"]
        name = item["name"]
        market = item["market"]
        print(f"  📊 [{name}] 获取中...")
        point = fetcher.fetch(code, name, market)
        if point:
            results[code] = point
            direction = "🟢" if point.daily_change >= 0 else "🔴"
            print(f"    ✅ {name}: {point.net_value} ({direction} {point.daily_change:+.2f}%)")
        else:
            print(f"    ❌ {name}: 获取失败")

    # 获取商品
    for item in commodities_cfg:
        code = item["code"]
        name = item["name"]
        market = item["market"]
        print(f"  🥇 [{name}] 获取中...")
        point = fetcher.fetch(code, name, market)
        if point:
            results[f"commodity:{code}"] = point
            direction = "🟢" if point.daily_change >= 0 else "🔴"
            print(f"    ✅ {name}: {point.net_value} ({direction} {point.daily_change:+.2f}%)")
        else:
            print(f"    ❌ {name}: 获取失败")

    return results
