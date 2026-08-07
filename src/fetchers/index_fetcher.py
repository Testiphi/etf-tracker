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
        elif market in ("us_index", "hk_index"):
            # 港股指数（恒生科技等）走同样的雅虎→腾讯多源逻辑
            return self._fetch_us_index(code, name)
        elif market == "cn_gold":
            return self._fetch_cn_gold(code, name)
        else:
            print(f"  ⚠ 未知市场类型: {market} (code={code})")
            return None

    # ── 中国A股指数 ──────────────────────────────────

    def _fetch_cn_index(self, code: str, name: str) -> FundDataPoint | None:
        """获取A股指数数据（东方财富 → 腾讯财经 双源）"""
        point = self._try_em_cn_index(code, name)
        if point:
            return point

        point = self._try_tencent_cn_index(code, name)
        if point:
            return point

        print(f"  ⚠ 所有A股指数数据源均失败 [{name}]")
        return None

    def _try_em_cn_index(self, code: str, name: str) -> FundDataPoint | None:
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

    def _try_tencent_cn_index(self, code: str, name: str) -> FundDataPoint | None:
        """通过腾讯财经 K线 API 获取A股指数（东方财富不可用时的 fallback）

        腾讯 A 股指数代码格式：sh000688 / sh000510（上证前缀 sh，深证 sz）
        day 行格式: [date, close, open, high, low, volume]
        涨跌幅用前一交易日收盘价计算，比 open 对比更准确。
        """
        if not code.isdigit():
            return None
        # 上证指数 000xxx 开头用 sh，深证指数 399xxx 用 sz；A股指数多为 000/399 开头
        prefix = "sz" if code.startswith("399") else "sh"
        tc_code = f"{prefix}{code}"

        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tc_code},day,,,320,qfq"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()

            day_data = data.get("data", {}).get(tc_code, {}).get("day", [])
            if not day_data:
                return None

            last = day_data[-1]
            if len(last) < 6:
                return None

            date_str = last[0]
            close_price = float(last[1])
            volume = float(last[5]) if last[5] else 0.0

            # 用前一交易日收盘价计算涨跌幅
            change_pct = 0.0
            if len(day_data) >= 2:
                prev_close = float(day_data[-2][1])
                if prev_close > 0:
                    change_pct = round((close_price - prev_close) / prev_close * 100, 2)

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
            print(f"  ⚠ 腾讯A股指数失败 [{name}]: {e}")
            return None

    # ── 美股指数 ──────────────────────────────────

    # 腾讯财经 US/HK 代码映射
    _TENCENT_US_MAP = {
        "^GSPC": "usINX",     # 标普500
        "^NDX": "usNDX",      # 纳斯达克100
        "^IXIC": "usIXIC",    # 纳斯达克综合（备用）
        "^DJI": "usDJI",      # 道琼斯（备用）
        "^HSTECH": "hkHSTECH",  # 恒生科技指数
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

    # ── 黄金行情 ──────────────────────────────────

    GOLD_API_URL = "https://api.gold-api.com/price/XAU"
    # 新浪行情：一次请求同时取 USD/CNY 汇率 + 伦敦金 XAU（hf_XAU 含昨收，用于计算涨跌幅）
    SINA_FX_URL = "https://hq.sinajs.cn/list=fx_susdcny,hf_XAU"
    _OZ_TO_GRAM = 31.1035  # 1 金衡盎司 = 31.1035 克

    def _fetch_cn_gold(self, code: str, name: str) -> FundDataPoint | None:
        """
        获取黄金行情（人民币/克）

        策略：
          1. gold-api.com 获取国际金价 XAU/USD（免 API Key）
          2. 新浪财经获取 USD/CNY 在岸汇率
          3. 换算：人民币每克价格 = XAU/USD × USD/CNY ÷ 31.1035
        """
        point = self._fetch_gold_via_api(code, name)
        if point:
            return point

        print(f"  ⚠ 所有黄金数据源均失败 [{name}]")
        return None

    def _fetch_gold_via_api(self, code: str, name: str) -> FundDataPoint | None:
        """通过 gold-api.com + 新浪汇率获取黄金人民币价格"""
        try:
            # 1. 获取 XAU/USD 国际金价
            resp_gold = requests.get(
                self.GOLD_API_URL,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=self.timeout,
            )
            resp_gold.raise_for_status()
            gold_data = resp_gold.json()
            xau_usd = float(gold_data.get("price", 0))
            if xau_usd <= 0:
                raise ValueError(f"Invalid gold price: {xau_usd}")

            # 2. 获取 USD/CNY 汇率 + 伦敦金昨收（新浪财经，一次请求两个行情）
            resp_fx = requests.get(
                self.SINA_FX_URL,
                headers={"Referer": "https://finance.sina.com.cn"},
                timeout=self.timeout,
            )
            resp_fx.raise_for_status()
            text = resp_fx.text
            # 解析新浪格式: var hq_str_fx_susdcny="16:58:04,6.7995,..."
            import re
            match = re.search(r'hq_str_fx_susdcny="([^"]+)"', text)
            if not match:
                raise ValueError(f"Cannot parse Sina FX: {text[:100]}")
            fields = match.group(1).split(",")
            if len(fields) < 2:
                raise ValueError(f"Unexpected FX format: {fields}")
            usd_cny = float(fields[1])
            if usd_cny <= 0:
                raise ValueError(f"Invalid USD/CNY: {usd_cny}")

            # 解析新浪 hf_XAU 昨收（伦敦金 XAU/USD 前一交易日收盘价）
            # 格式: var hq_str_hf_XAU="最新价,昨收,今开,最高,最低,时间,买价,卖价,..."
            prev_xau_usd = None
            m_gold = re.search(r'hq_str_hf_XAU="([^"]+)"', text)
            if m_gold:
                g = m_gold.group(1).split(",")
                if len(g) > 1:
                    try:
                        prev_xau_usd = float(g[1])
                    except ValueError:
                        prev_xau_usd = None

            # 3. 换算为人民币/克
            price_cny_gram = round(xau_usd * usd_cny / self._OZ_TO_GRAM, 2)

            # 4. 计算涨跌幅：优先用新浪伦敦金昨收（API 自带，不依赖本地历史库，
            #    CI 每次全新空库也能算对）；历史库仅作兜底
            change_pct = 0.0
            if prev_xau_usd and prev_xau_usd > 0:
                change_pct = round((xau_usd - prev_xau_usd) / prev_xau_usd * 100, 2)
            else:
                try:
                    from ..storage import get_latest_price_for_code
                    # 兼容两种存储键：新版并入 indices 后存 "XAU"，旧数据为 "commodity:XAU"
                    prev_close = get_latest_price_for_code(code) or \
                        get_latest_price_for_code(f"commodity:{code}")
                    if prev_close and prev_close > 0:
                        change_pct = round((price_cny_gram - prev_close) / prev_close * 100, 2)
                except Exception:
                    pass

            now = datetime.now(timezone(timedelta(hours=8)))
            print(f"    🥇 XAU/USD=${xau_usd:.2f} 昨收=${prev_xau_usd or 0:.2f} USD/CNY={usd_cny:.4f} → ¥{price_cny_gram:.2f}/克 ({change_pct:+.2f}%)")

            return FundDataPoint(
                code=code,
                name=f"国际金价(¥/克)",
                date=gold_data.get("updatedAt", now.strftime("%Y-%m-%d"))[:10],
                net_value=price_cny_gram,
                acc_value=0,
                daily_change=change_pct,
                volume=0,
                source="gold-api+sina",
            )
        except Exception as e:
            print(f"  ⚠ 黄金API获取失败: {e}")
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
