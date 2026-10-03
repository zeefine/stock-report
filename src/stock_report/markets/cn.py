#!/usr/bin/env python3
"""Generate the latest A-share close report with public, auditable endpoints."""
from __future__ import annotations

import hashlib, html, json, re, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from stock_report.metrics import technical_snapshot as _technical_snapshot
except ModuleNotFoundError:  # defensive fallback for standalone module execution
    _technical_snapshot = None
from stock_report.news import load_news_events
from stock_report.analysis import cn_market_assessment
from stock_report.render import html_table as _shared_html_table, render_template
from stock_report.common import (
    cn_session_state, cn_timestamp, cn_close_quote_errors, cn_close_minute_checks, cn_close_price_mismatches,
    CN_MINUTE_INDEXES, finite_number, latest_completed_us_session, us_session_close, cn_minute_rows_complete,
)

ROOT = Path(__file__).resolve().parents[3]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
TEMPLATE = ROOT / "A股收盘日报_template.html"
REPORTS = ROOT / "reports"
DATA_ROOT = ROOT / "runs" / "cn"
SH_TZ = ZoneInfo("Asia/Shanghai")

INDEXES = [
    ("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sh000300", "沪深300"),
    ("sh000905", "中证500"), ("sz399852", "中证1000"), ("sz399006", "创业板指"),
    ("sh000688", "科创50"), ("sh000016", "上证50"),
]
INTRADAY_INDEXES = list(CN_MINUTE_INDEXES)
POOL = [
    "600519","300750","601318","600036","000333","002594","601138","300308",
    "002475","688041","688256","002371","000858","603288","600276","300760",
    "000963","600030","601166","601899","600309","601857","600900","600028",
    "601088","601985","600886","000001","300059","600111","300124",
]
SECTOR_BASKETS = {
    "科技与高端制造": ["601138","300308","002475","688041","688256","002371"],
    "消费": ["600519","000858","603288","000333","002594"],
    "医药": ["600276","300760","000963"],
    "金融": ["601318","600036","600030","601166"],
    "周期资源": ["601899","600309","601857","600028"],
    "电力与高股息": ["600900","601985","600886","601088"],
}

def request(url: str, params: dict | None = None, data: bytes | None = None,
            headers: dict | None = None, timeout: int = 20, tries: int = 2):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    h = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}
    if headers: h.update(headers)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            if i + 1 < tries: time.sleep(1.2 + i * 0.8)
    raise last

def get_json(url: str, params: dict | None = None, **kw):
    return json.loads(request(url, params=params, **kw).decode("utf-8", "replace"))

def get_text(url: str, params: dict | None = None, **kw):
    return request(url, params=params, **kw).decode("utf-8", "replace")

def read_json_file(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def code_prefix(code: str) -> str:
    c = code.lower().replace("sh", "").replace("sz", "").replace("bj", "")
    if c.startswith(("92", "8")): return "bj"
    if c.startswith(("5", "6", "9")): return "sh"
    return "sz"


def market_symbol(code: str) -> str:
    """Keep explicit index markets; infer the market only for bare stock codes."""
    code = code.lower()
    return code if code.startswith(("sh", "sz", "bj")) else code_prefix(code) + code

def fmt_num(x, digits=2):
    if x is None: return "未取得"
    return f"{x:,.{digits}f}"

def fmt_pct(x, digits=2):
    if x is None: return '<span class="muted">未取得</span>'
    cls = "up" if x > 0 else "down" if x < 0 else "muted"
    return f'<span class="{cls}">{x:+.{digits}f}%</span>'

def fmt_money_yi(x):
    return "未取得" if x is None else f"{x/1e8:,.2f}亿"


def fmt_amount_wan(amount_wan):
    """Format Tencent's 万元 amount without converting a missing value to zero."""
    return fmt_money_yi(amount_wan * 1e4) if amount_wan is not None else "未取得"

def pct(a, b):
    return (a / b - 1) * 100 if a is not None and b not in (None, 0) else None

def tencent_quotes(codes: list[str]) -> dict[str, dict]:
    keys, query = {}, []
    for raw in codes:
        code = raw.lower()
        if code.startswith(("sh", "sz", "bj")):
            p = code
        else:
            p = code_prefix(code) + code
        query.append(p); keys[p] = raw
    # 腾讯行情返回 GBK；按 UTF-8 解码会把中文公司名变成乱码。
    text = request("http://qt.gtimg.cn/q=" + ",".join(query), timeout=15).decode("gbk", "replace")
    out = {}
    for line in text.split(";"):
        if "=" not in line or '"' not in line: continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 50: continue
        def f(i, default=None):
            try: return float(vals[i]) if vals[i] else default
            except Exception: return default
        raw = keys.get(key, key[2:] if len(key) > 2 else key)
        out[raw] = {
            "name": vals[1], "price": f(3), "last_close": f(4), "open": f(5),
            "change_amt": f(31), "change_pct": f(32), "high": f(33), "low": f(34),
            "amount_wan": f(37), "turnover_pct": f(38), "pe_ttm": f(39),
            "mcap_yi": f(45), "pb": f(46), "limit_up": f(47), "limit_down": f(48),
            "vol_ratio": f(49), "time": vals[30] if len(vals) > 30 else "",
        }
    return out

def em_quotes(codes: list[str]) -> dict[str, dict]:
    symbols_by_secid = {}
    for raw in codes:
        symbol = market_symbol(raw)
        secid = ("1" if symbol.startswith("sh") else "0") + "." + symbol[2:]
        symbols_by_secid[secid] = symbol
    params = {"fltt": "2", "invt": "2", "fields": "f2,f3,f4,f12,f13,f14,f104,f105",
             "secids": ",".join(symbols_by_secid)}
    d = get_json("https://push2.eastmoney.com/api/qt/ulist.np/get", params=params,
                 headers={"Referer": "https://quote.eastmoney.com/"}, timeout=15)
    diff = ((d.get("data") or {}).get("diff") or [])
    if isinstance(diff, dict): diff = list(diff.values())
    out = {}
    for x in diff:
        symbol = symbols_by_secid.get(f"{x.get('f13')}.{x.get('f12')}")
        if symbol is None:
            # Never guess from a six-digit code or the order of returned rows.
            continue
        out[symbol] = {"name": x.get("f14", ""), "price": x.get("f2"), "change_pct": x.get("f3"),
                  "change_amt": x.get("f4"), "up_count": x.get("f104"), "down_count": x.get("f105")}
    return out

def kline(symbol: str, n: int = 320, adjustment: str = "qfq") -> list[dict]:
    p = symbol.lower() if symbol[:2].lower() in ("sh", "sz", "bj") else code_prefix(symbol) + symbol
    try:
        # 日线端点偶尔会对某一标的长时间不返回。它仅用于技术指标，
        # 因而应尽快降级为缺失，不能阻塞整份日报的收盘事实数据。
        d = get_json(
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={p},day,,,{n},{adjustment}",
            timeout=8,
            tries=1,
        )
        block = (d.get("data") or {}).get(p) or {}
        # 指数返回 day，个股前复权返回 qfqday。
        raw = block.get("day") or (block.get("qfqday") if adjustment else []) or []
        rows=[]
        for x in raw:
            if len(x) < 6: continue
            try: rows.append({"date": x[0], "open": float(x[1]), "close": float(x[2]), "high": float(x[3]), "low": float(x[4]), "volume": float(x[5])})
            except Exception: pass
        return rows
    except Exception: return []


def fetch_klines(symbols: list[str], n: int = 320, max_workers: int = 8,
                 adjustment: str = "qfq") -> dict[str, list[dict]]:
    """Fetch independent daily series concurrently; a failed symbol remains an empty series."""
    unique = list(dict.fromkeys(symbols))
    with ThreadPoolExecutor(max_workers=min(max_workers, len(unique) or 1)) as pool:
        rows = list(pool.map(lambda symbol: kline(symbol, n, adjustment=adjustment), unique))
    return dict(zip(unique, rows))

def minute_kline(symbol: str, interval: str = "m5", n: int = 320) -> list[dict]:
    """腾讯分钟K。成交量单位为手；成交额使用OHLC均价估算，不能误读第7字段。"""
    p = symbol.lower() if symbol[:2].lower() in ("sh", "sz", "bj") else code_prefix(symbol) + symbol
    try:
        d = get_json(
            f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={p},{interval},,{n}",
            headers={"Referer": "https://gu.qq.com/"}, timeout=20,
        )
        raw = ((d.get("data") or {}).get(p) or {}).get(interval) or []
        rows = []
        for x in raw:
            if len(x) < 6:
                continue
            try:
                dt = datetime.strptime(x[0], "%Y%m%d%H%M").replace(tzinfo=SH_TZ)
                op, close, high, low, volume = map(float, x[1:6])
                average_price = (op + close + high + low) / 4
                rows.append({
                    "time": dt.isoformat(),
                    "minute": dt.strftime("%H:%M"),
                    "open": op, "close": close, "high": high, "low": low,
                    "volume_lots": volume,
                    "estimated_amount": volume * 100 * average_price,
                    "turnover_basis_points": float(x[7]) if len(x) > 7 and x[7] not in ("", None) else None,
                })
            except Exception:
                continue
        return rows
    except Exception:
        return []

def _cache_fresh(path: Path, ttl_seconds: int, now: datetime) -> bool:
    if not path.exists():
        return False
    modified = datetime.fromtimestamp(path.stat().st_mtime, SH_TZ)
    return (now - modified).total_seconds() <= ttl_seconds


def _truncate_intraday_pack(pack: dict, as_of: datetime | None, key: str) -> dict:
    """Return a cutoff-safe view of cached intraday data without overwriting the raw cache."""
    if as_of is None:
        return pack
    trimmed = {**pack, "as_of": as_of.isoformat()}
    if key == "symbols":
        trimmed["symbols"] = {
            symbol: [
                row for row in rows
                if datetime.fromisoformat(row["time"]) <= as_of
            ]
            for symbol, rows in (pack.get("symbols") or {}).items()
        }
    else:
        trimmed["events"] = [
            event for event in (pack.get("events") or [])
            if datetime.fromisoformat(event["published_at"]) <= as_of
        ]
    return trimmed


def collect_intraday_klines(
    report_date: str,
    data_dir: Path,
    *,
    mode: str = "close",
    as_of: datetime | None = None,
    refresh: bool = False,
    cache_seconds: int = 120,
) -> dict:
    cache = data_dir / "minute_indices.json"
    old = read_json_file(cache, {})
    old = old if isinstance(old, dict) else {}
    cached_symbols = old.get("symbols")
    if (refresh or old.get("report_date") != report_date
            or old.get("interval", "5m") != "5m" or not isinstance(cached_symbols, dict)):
        cached_symbols = {}
    now = datetime.now(SH_TZ)
    if mode == "intraday" and cached_symbols and _cache_fresh(cache, cache_seconds, now):
        return _truncate_intraday_pack(old, as_of, "symbols")
    # Cache completeness is independent of quote agreement (checked at QC).
    # Retain complete symbols, but retry failed/partial symbols on every close run.
    complete = cn_close_minute_checks(cached_symbols, {}, report_date) if mode == "close" else {}
    if complete and all(check["complete"] for check in complete.values()):
        return {**old, "symbols": {s: sorted(cached_symbols[s], key=lambda r: cn_timestamp(r["time"]))
                                    for s in INTRADAY_INDEXES}}
    symbols = {}
    for symbol in INTRADAY_INDEXES:
        if complete.get(symbol, {}).get("complete"):
            symbols[symbol] = sorted(cached_symbols[symbol], key=lambda r: cn_timestamp(r["time"]))
            continue
        rows = [x for x in minute_kline(symbol) if x["time"][:10] == report_date]
        if mode == "intraday" and as_of is not None:
            rows = [x for x in rows if datetime.fromisoformat(x["time"]) <= as_of]
        symbols[symbol] = rows
    pack = {
        "schema_version": "1.0",
        "report_date": report_date,
        "interval": "5m",
        "timezone": "Asia/Shanghai",
        "mode": mode,
        "as_of": as_of.isoformat() if as_of else None,
        "source": "Tencent mkline",
        "generated_at": datetime.now(SH_TZ).isoformat(),
        "symbols": symbols,
        "field_notes": {
            "volume_lots": "手",
            "estimated_amount": "成交量(手)×100×OHLC均价，仅为估算",
            "turnover_basis_points": "腾讯原始第7字段为换手率基点，不是成交额",
        },
    }
    cache.write_text(json.dumps(pack, ensure_ascii=False, indent=2))
    return pack

def _cls_page(last_time: str = "", page_size: int = 50) -> list[dict]:
    params = {
        "app": "CailianpressWeb", "os": "web", "sv": "7.7.5",
        "last_time": last_time, "refresh_type": "1", "rn": str(page_size),
    }
    query = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sign = hashlib.md5(hashlib.sha1(query.encode()).hexdigest().encode()).hexdigest()
    try:
        d = get_json(
            f"https://www.cls.cn/v1/roll/get_roll_list?{query}&sign={sign}",
            headers={"Referer": "https://www.cls.cn/"}, timeout=20,
        )
        return ((d.get("data") or {}).get("roll_data") or [])
    except Exception:
        return []

def _cls_is_a_share_relevant(item: dict) -> bool:
    subjects = {
        str(x.get("subject_name") or "")
        for x in (item.get("subjects") or []) if isinstance(x, dict)
    }
    if subjects and subjects.issubset({"美股动态", "港股动态"}):
        return False
    if item.get("stock_list"):
        return True
    text = " ".join(str(item.get(k) or "") for k in ("title", "brief", "content"))
    keywords = (
        "A股", "沪深", "上证", "深证", "创业板", "科创板", "北交所",
        "中国", "国内", "国务院", "央行", "证监会", "财政部", "商务部",
        "发改委", "工信部", "国家统计局", "人民币", "沪市", "深市",
    )
    return any(k in text for k in keywords)

def collect_timestamped_events(
    report_date: str,
    data_dir: Path,
    max_pages: int = 16,
    *,
    mode: str = "close",
    as_of: datetime | None = None,
    refresh: bool = False,
    cache_seconds: int = 120,
) -> dict:
    """获取报告日财联社盘中快讯。快讯时间是候选证据，不自动等于事件发生时间。"""
    cache = data_dir / "intraday_events.json"
    old = read_json_file(cache, {})
    now = datetime.now(SH_TZ)
    reusable = mode == "close" or _cache_fresh(cache, cache_seconds, now)
    if not refresh and reusable and old.get("report_date") == report_date and old.get("events"):
        return _truncate_intraday_pack(old, as_of if mode == "intraday" else None, "events")
    target = datetime.strptime(report_date, "%Y-%m-%d").replace(tzinfo=SH_TZ)
    stop_at = target.replace(hour=9, minute=0)
    seen, events, last_time = set(), [], ""
    for _ in range(max_pages):
        page = _cls_page(last_time)
        if not page:
            break
        for item in page:
            item_id = str(item.get("id") or "")
            if item_id in seen:
                continue
            seen.add(item_id)
            ts = item.get("ctime")
            if not ts:
                continue
            published = datetime.fromtimestamp(float(ts), SH_TZ)
            minutes = published.hour * 60 + published.minute
            in_session = (9 * 60 + 15 <= minutes <= 11 * 60 + 30) or (
                13 * 60 <= minutes <= 15 * 60
            )
            within_cutoff = mode != "intraday" or as_of is None or published <= as_of
            if published.strftime("%Y-%m-%d") == report_date and in_session and within_cutoff:
                title = item.get("title") or item.get("brief") or item.get("content") or ""
                subjects = [
                    x.get("subject_name", "") for x in (item.get("subjects") or [])
                    if isinstance(x, dict)
                ]
                events.append({
                    "event_id": f"cls-{item_id}",
                    "title": re.sub(r"\s+", " ", str(title)).strip(),
                    "published_at": published.isoformat(),
                    "event_at": None,
                    "retrieved_at": datetime.now(SH_TZ).isoformat(),
                    "source": "财联社电报",
                    "source_type": "wire",
                    "source_url": item.get("shareurl") or f"https://www.cls.cn/detail/{item_id}",
                    "subjects": subjects,
                    "stock_list": item.get("stock_list") or [],
                    "a_share_relevant": _cls_is_a_share_relevant(item),
                    "verification": "timestamped_candidate",
                })
        oldest = min(
            (datetime.fromtimestamp(float(x["ctime"]), SH_TZ) for x in page if x.get("ctime")),
            default=None,
        )
        if oldest and oldest <= stop_at:
            break
        next_cursor = min((int(x.get("ctime")) for x in page if x.get("ctime")), default=None)
        if next_cursor is None:
            break
        last_time = str(next_cursor - 1)
    events.sort(key=lambda x: x["published_at"])
    pack = {
        "schema_version": "1.0",
        "report_date": report_date,
        "timezone": "Asia/Shanghai",
        "mode": mode,
        "as_of": as_of.isoformat() if as_of else None,
        "source": "财联社电报",
        "generated_at": datetime.now(SH_TZ).isoformat(),
        "events": events,
        "notes": "published_at为快讯发布时间；event_at缺失时不得直接认定为事件真实发生时间或价格因果。",
    }
    cache.write_text(json.dumps(pack, ensure_ascii=False, indent=2))
    return pack

def rsi(values, n=14):
    if len(values) < n + 1: return None
    gains=[]; losses=[]
    for a,b in zip(values[-n-1:-1], values[-n:]):
        d=b-a; gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/n; al=sum(losses)/n
    return 100 if al == 0 else 100 - 100/(1 + ag/al)

def technical(rows: list[dict], report_date: str) -> dict:
    rows = [r for r in rows if r["date"] <= report_date]
    close = [r["close"] for r in rows]
    last = rows[-1] if rows else None
    if _technical_snapshot is not None:
        z = _technical_snapshot(rows)
        macd = None
        if len(close) >= 26:
            macd = "偏强" if sum(close[-12:]) / 12 > sum(close[-26:]) / 26 else "偏弱"
        return {
            "close": z["close"], "day": z["day"], "r5": z["r5"],
            "r20": pct(close[-1], close[-21]) if len(close) > 20 else None,
            "ma20": z["ma20"], "ma50": z["ma50"], "ma200": z["ma200"],
            "rsi": z["rsi14"], "macd": macd, "vol_ratio": z["volume_ratio20"],
            "last_date": z["last_date"],
        }
    def ma(n): return sum(close[-n:]) / n if len(close) >= n else None
    vr = None
    if len(rows) >= 21:
        base = sum(r["volume"] for r in rows[-21:-1]) / 20
        vr = rows[-1]["volume"] / base if base else None
    macd = None
    if len(close) >= 26:
        ema12 = sum(close[-12:]) / 12; ema26 = sum(close[-26:]) / 26
        macd = "偏强" if ema12 > ema26 else "偏弱"
    return {"close": last["close"] if last else None, "day": pct(close[-1], close[-2]) if len(close)>1 else None,
            "r5": pct(close[-1], close[-6]) if len(close)>5 else None, "r20": pct(close[-1], close[-21]) if len(close)>20 else None,
            "ma20": ma(20), "ma50": ma(50), "ma200": ma(200), "rsi": rsi(close), "macd": macd, "vol_ratio": vr,
            "last_date": last["date"] if last else None}

def industry_rows():
    params={"pn":"1","pz":"100","po":"1","np":"1","fltt":"2","invt":"2","fid":"f3","fs":"m:90+t:2",
            "fields":"f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207"}
    try:
        d=get_json("https://push2.eastmoney.com/api/qt/clist/get",params=params,
                   headers={"Referer":"https://quote.eastmoney.com/"},timeout=15)
        items=((d.get("data") or {}).get("diff") or [])
        return [{"name":x.get("f14",""),"change_pct":x.get("f3"),"up":x.get("f104"),"down":x.get("f105"),"leader":x.get("f140","")} for x in items]
    except Exception: return []

def board_flow(board_type="industry", period="today"):
    fs={"industry":"m:90+t:2","concept":"m:90+t:3"}.get(board_type)
    if not fs: return []
    fid="f62" if period=="today" else "f164" if period=="5d" else "f174"
    chg="f3" if period=="today" else "f109" if period=="5d" else "f160"
    fields=",".join(["f12","f14",chg,fid,"f184"] + (["f66","f72","f78","f84","f204"] if period=="today" else []))
    params={"pn":"1","pz":"20","po":"1","np":"1","fltt":"2","invt":"2","fid":fid,"fs":fs,"fields":fields}
    try:
        d=get_json("https://push2.eastmoney.com/api/qt/clist/get",params=params,headers={"Referer":"https://quote.eastmoney.com/"},timeout=15)
        items=((d.get("data") or {}).get("diff") or [])
        return [{"name":x.get("f14",""),"change":x.get(chg),"main_net":x.get(fid),"main_pct":x.get("f184"),"leader":x.get("f204","")} for x in items]
    except Exception: return []

def pool(endpoint, date_str):
    url=f"https://push2ex.eastmoney.com/{endpoint}"
    params={"ut":"7eea3edcaed734bea9cbfc24409ed989","dpt":"wz.ztzt","Pageindex":0,"pagesize":10000,"sort":"fbt:asc","date":date_str.replace("-","")}
    try:
        d=get_json(url,params=params,headers={"Referer":"https://quote.eastmoney.com/"},timeout=15)
        if not isinstance(d, dict) or type(d.get("rc")) is not int or d["rc"] != 0:
            return None
        data = d.get("data")
        rows = data.get("pool") if isinstance(data, dict) else None
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not re.fullmatch(r"\d{6}", str(row.get("c", "")))
            for row in rows
        ):
            return None
        return rows  # An explicitly successful empty pool is a real zero.
    except Exception:
        return None

def dragon_tiger(date_str):
    params={"reportName":"RPT_DAILYBILLBOARD_DETAILSNEW","columns":"ALL","filter":f"(TRADE_DATE>='{date_str}')(TRADE_DATE<='{date_str}')","pageNumber":"1","pageSize":"500","sortColumns":"BILLBOARD_NET_AMT","sortTypes":"-1","source":"WEB","client":"WEB"}
    try:
        d=get_json("https://datacenter-web.eastmoney.com/api/data/v1/get",params=params,headers={"Referer":"https://data.eastmoney.com/"},timeout=20)
        rows=((d.get("result") or {}).get("data") or [])
        return [{"code":x.get("SECURITY_CODE",""),"name":x.get("SECURITY_NAME_ABBR",""),"reason":x.get("EXPLANATION",""),"net":(x.get("BILLBOARD_NET_AMT") or 0)/1e4,"change":x.get("CHANGE_RATE"),"turnover":x.get("TURNOVERRATE")} for x in rows]
    except Exception:return []

_CNINFO_ORG_IDS = {}

def cninfo_org_id(code: str) -> str:
    global _CNINFO_ORG_IDS
    if not _CNINFO_ORG_IDS:
        try:
            d = get_json(
                "http://www.cninfo.com.cn/new/data/szse_stock.json",
                headers={"Referer": "https://www.cninfo.com.cn/"},
                timeout=15,
            )
            _CNINFO_ORG_IDS = {
                str(x.get("code")): str(x.get("orgId"))
                for x in (d.get("stockList") or []) if x.get("code") and x.get("orgId")
            }
        except Exception:
            _CNINFO_ORG_IDS = {}
    if code in _CNINFO_ORG_IDS:
        return _CNINFO_ORG_IDS[code]
    if code.startswith("6"):
        return "gssh0" + code
    if code.startswith(("4", "8", "92")):
        return "gsbj0" + code
    return "gssz0" + code

def announcements(code):
    org = cninfo_org_id(code)
    body=urllib.parse.urlencode({"stock":f"{code},{org}","tabName":"fulltext","pageSize":"10","pageNum":"1","column":"","category":"","plate":"","seDate":"","searchkey":"","secid":"","sortName":"","sortType":"","isHLtitle":"true"}).encode()
    try:
        d=get_json(
            "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            data=body,
            headers={"Content-Type":"application/x-www-form-urlencoded","Referer":"https://www.cninfo.com.cn/new/disclosure"},
            timeout=6,
            tries=1,
        )
        out = []
        for x in (d.get("announcements") or [])[:5]:
            ts = x.get("announcementTime")
            published = (
                datetime.fromtimestamp(float(ts) / 1000, SH_TZ).isoformat()
                if isinstance(ts, (int, float)) else None
            )
            out.append({
                "title": x.get("announcementTitle", ""),
                "type": x.get("announcementTypeName", ""),
                "published_at": published,
                "source": "巨潮资讯",
                "verification": "official_announcement",
                "url": f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={x.get('announcementId','')}",
            })
        return out
    except Exception:return []


def fetch_announcements(codes: list[str], max_workers: int = 4) -> dict[str, list[dict]]:
    """Supplement disclosures without allowing a slow issuer endpoint to hold up the report."""
    unique = list(dict.fromkeys(codes))
    with ThreadPoolExecutor(max_workers=min(max_workers, len(unique) or 1)) as pool:
        rows = list(pool.map(announcements, unique))
    return dict(zip(unique, rows))

def news_events(news_dir: Path):
    return load_news_events(news_dir)

def _rows_for_symbol(minute_pack: dict, symbol: str) -> list[dict]:
    return ((minute_pack.get("symbols") or {}).get(symbol) or [])

def _row_at_or_before(rows: list[dict], target: datetime):
    eligible = [x for x in rows if datetime.fromisoformat(x["time"]) <= target]
    return eligible[-1] if eligible else None

def _row_at_or_after(rows: list[dict], target: datetime):
    return next((x for x in rows if datetime.fromisoformat(x["time"]) >= target), None)

def _window_return(rows: list[dict], start: datetime, end: datetime):
    first = _row_at_or_after(rows, start)
    last = _row_at_or_before(rows, end)
    if not first or not last or first is last:
        return None
    return pct(last["close"], first["close"])

def _expected_cn_bars(report_date: str, as_of: datetime) -> list[datetime]:
    day = datetime.strptime(report_date, "%Y-%m-%d").replace(tzinfo=SH_TZ)
    bars = []
    for start, end in ((day.replace(hour=9, minute=35), day.replace(hour=11, minute=30)),
                       (day.replace(hour=13, minute=5), day.replace(hour=15, minute=0))):
        current = start
        while current <= end:
            if current <= as_of:
                bars.append(current)
            current += timedelta(minutes=5)
    return bars


def intraday_quality(
    report_date: str,
    minute_pack: dict,
    events_pack: dict,
    quotes: dict,
    *,
    mode: str = "close",
    as_of: datetime | None = None,
) -> dict:
    as_of = as_of or datetime.strptime(report_date, "%Y-%m-%d").replace(
        hour=15, minute=0, tzinfo=SH_TZ
    )
    expected_bars = _expected_cn_bars(report_date, as_of)
    minimum_bars = 48 if mode == "close" else len(expected_bars)
    required_end = "15:00" if mode == "close" else (
        expected_bars[-1].strftime("%H:%M") if expected_bars else None
    )
    checks = {}
    valid = 0
    close_checks = cn_close_minute_checks(minute_pack.get("symbols") or {}, quotes, report_date) if mode == "close" else {}
    for symbol in INTRADAY_INDEXES:
        rows = _rows_for_symbol(minute_pack, symbol)
        rows = rows if isinstance(rows, list) else []
        first_time = cn_timestamp(rows[0].get("time")) if rows and isinstance(rows[0], dict) else None
        last_time = cn_timestamp(rows[-1].get("time")) if rows and isinstance(rows[-1], dict) else None
        first = first_time.strftime("%H:%M") if first_time else ""
        last = last_time.strftime("%H:%M") if last_time else ""
        quote_close = (quotes.get(symbol) or {}).get("price")
        minute_close = rows[-1].get("close") if rows and isinstance(rows[-1], dict) else None
        close_error = abs(pct(minute_close, quote_close) or 0) if finite_number(minute_close) and finite_number(quote_close) and quote_close > 0 else None
        if mode == "close":
            ok = close_checks[symbol]["valid"]
        else:
            ok = cn_minute_rows_complete(rows, expected_bars)
        valid += int(ok)
        checks[symbol] = {
            "bars": len(rows), "first": first or None, "last": last or None,
            "minute_close": minute_close, "daily_close": quote_close,
            "close_error_pct": close_error, "valid": ok,
        }
    relevant_events = [
        e for e in (events_pack.get("events") or []) if e.get("a_share_relevant")
    ]
    coverage = valid / len(INTRADAY_INDEXES) if INTRADAY_INDEXES else 0
    ready = coverage >= 0.8 and (mode == "intraday" or len(relevant_events) >= 3)
    return {
        "mode": mode,
        "as_of": as_of.isoformat(),
        "session_state": cn_session_state(as_of),
        "is_final": (mode == "close" and coverage >= 0.8
                     and not cn_close_quote_errors(quotes, as_of)
                     and not cn_close_price_mismatches(close_checks)),
        "ready": ready,
        "core_index_coverage": coverage,
        "valid_index_count": valid,
        "required_index_count": len(INTRADAY_INDEXES),
        "timestamped_event_count": len(events_pack.get("events") or []),
        "a_share_relevant_event_count": len(relevant_events),
        "checks": checks,
        "rules": {
            "minimum_bars_per_index": minimum_bars,
            "required_session_start": "09:35",
            "required_session_end": required_end,
            "maximum_close_error_pct": 0.2 if mode == "close" else None,
            "minimum_core_index_coverage": 0.8,
            "minimum_relevant_timestamped_events": 3 if mode == "close" else 0,
        },
    }

def _event_scope(title: str) -> str:
    """只把有明确国内官方主体的消息送入宽基指数事件对齐。"""
    if "全球央行" in title:
        return "global_macro"
    official_terms = (
        "中共中央", "政治局", "国务院", "中国人民银行", "证监会",
        "财政部", "国家发展改革委", "发改委", "商务部", "国家统计局",
        "工信部", "海关总署",
    )
    if any(x in title for x in official_terms):
        return "macro_policy"
    observation_terms = (
        "成交额突破", "指数涨", "指数跌", "板块表现", "板块走强",
        "板块走弱", "概念表现", "直线涨停", "涨近", "跌近", "涨超",
        "跌超", "拉升", "跳水",
    )
    if any(x in title for x in observation_terms):
        return "market_observation"
    return "company_or_other"

def _event_window(rows: list, published: datetime, cutoff: datetime | None):
    """Use a complete 30-minute five-minute-grid proxy, never a partial endpoint."""
    start = published.replace(minute=published.minute // 5 * 5, second=0, microsecond=0)
    end = start + timedelta(minutes=30)
    valid = [(cn_timestamp(row.get("time")), row) for row in rows if isinstance(row, dict)]
    valid = [(stamp, row) for stamp, row in valid
             if stamp and stamp.date() == published.date() and (cutoff is None or stamp <= cutoff)]
    expected = [start + timedelta(minutes=5 * i) for i in range(7)]
    window_rows = [row for stamp, row in valid if start <= stamp <= end]
    session_grid = set(_expected_cn_bars(published.date().isoformat(), end))
    complete = (bool(valid) and max(stamp for stamp, _ in valid) >= published + timedelta(minutes=30)
                and set(expected).issubset(session_grid)
                and cn_minute_rows_complete(window_rows, expected))
    observed_end = max((stamp for stamp, _ in valid if start <= stamp <= end), default=None)
    info = {"complete": complete, "actual_start": start.isoformat(),
            "actual_end": observed_end.isoformat() if observed_end else None,
            "observed_minutes": (observed_end - start).total_seconds() / 60 if observed_end else 0}
    if not complete:
        return None, None, info
    move = pct(window_rows[-1]["close"], window_rows[0]["close"])
    before_times = [start - timedelta(minutes=5 * i) for i in reversed(range(6))]
    before = [row for stamp, row in valid if before_times[0] <= stamp <= start]
    after = window_rows[1:]
    ratio = None
    if (set(before_times).issubset(session_grid) and cn_minute_rows_complete(before, before_times)
            and all(finite_number(row.get("volume_lots")) and row["volume_lots"] >= 0 for row in before + after)):
        before_sum = sum(row["volume_lots"] for row in before)
        ratio = sum(row["volume_lots"] for row in after) / before_sum if before_sum else None
    return move, ratio, info


def align_intraday_events(minute_pack: dict, events_pack: dict) -> list[dict]:
    """衡量快讯发布后30分钟的同步行情；结果只表示时间对齐，不表示因果。"""
    aligned = []
    for event in events_pack.get("events") or []:
        if not event.get("a_share_relevant"):
            continue
        published = cn_timestamp(event.get("published_at"))
        if published is None:
            continue
        moves, windows = {}, {}
        volume_ratio = None
        for symbol in ("sh000001", "sh000300", "sz399006", "sh000688"):
            rows = _rows_for_symbol(minute_pack, symbol)
            moves[symbol], ratio, windows[symbol] = _event_window(
                rows, published, cn_timestamp(minute_pack.get("as_of")))
            if symbol == "sh000001":
                volume_ratio = ratio
        max_move = max((abs(x) for x in moves.values() if x is not None), default=0)
        if all(move is None for move in moves.values()):
            strength = "窗口待完成或数据不足"
        elif max_move >= 0.5 and (volume_ratio or 0) >= 1.2:
            strength = "较强同步"
        elif max_move >= 0.3:
            strength = "一般同步"
        else:
            strength = "未见显著同步"
        aligned.append({
            "event_id": event["event_id"],
            "title": event["title"],
            "published_at": event["published_at"],
            "source": event["source"],
            "source_url": event.get("source_url"),
            "verification": event.get("verification"),
            "event_scope": _event_scope(event["title"]),
            "moves_30m_pct": moves,
            "windows": windows,
            "volume_ratio": volume_ratio,
            "max_abs_move_pct": max_move,
            "alignment_strength": strength,
            "causality": "not_confirmed",
        })
    aligned.sort(key=lambda x: x["max_abs_move_pct"], reverse=True)
    return aligned

def _phase_observation(sse, csi, growth):
    vals = [x for x in (sse, csi, growth) if x is not None]
    if len(vals) < 2:
        return "数据不足"
    if all(x > 0 for x in vals):
        return "主要指数同步上行"
    if all(x < 0 for x in vals):
        return "主要指数同步走弱"
    if growth is not None and csi is not None and growth > csi:
        return "成长相对占优"
    if growth is not None and csi is not None and growth < csi:
        return "权重相对占优"
    return "指数表现分化"

def build_intraday_section(report_date: str, minute_pack: dict, events_pack: dict,
                           quality: dict, quotes: dict, *, mode: str = "close",
                           as_of: datetime | None = None) -> tuple[str, list[dict]]:
    if not quality.get("ready"):
        return "", []
    day = datetime.strptime(report_date, "%Y-%m-%d").replace(tzinfo=SH_TZ)
    phases = [
        ("集合竞价/开盘", None, None),
        ("上午开盘", day.replace(hour=9, minute=35), day.replace(hour=10, minute=0)),
        ("上午中段", day.replace(hour=10, minute=0), day.replace(hour=11, minute=30)),
        ("午后开盘", day.replace(hour=13, minute=5), day.replace(hour=14, minute=0)),
        ("尾盘", day.replace(hour=14, minute=0), day.replace(hour=15, minute=0)),
    ]
    as_of = as_of or day.replace(hour=15, minute=0)
    rows = []
    for label, start, end in phases:
        if start is None:
            moves = {
                symbol: pct(
                    (quotes.get(symbol) or {}).get("open"),
                    (quotes.get(symbol) or {}).get("last_close"),
                )
                for symbol in ("sh000001", "sh000300", "sz399006")
            }
            period = "昨收→开盘"
        else:
            if mode == "intraday" and start > as_of:
                continue
            if mode == "intraday" and end > as_of:
                end = as_of
            moves = {
                symbol: _window_return(_rows_for_symbol(minute_pack, symbol), start, end)
                for symbol in ("sh000001", "sh000300", "sz399006")
            }
            period = f"{start:%H:%M}–{end:%H:%M}"
        rows.append([
            label, period, fmt_pct(moves["sh000001"]), fmt_pct(moves["sh000300"]),
            fmt_pct(moves["sz399006"]),
            _phase_observation(
                moves["sh000001"], moves["sh000300"], moves["sz399006"]
            ),
        ])
    timeline = html_table(
        ["阶段", "时间", "上证指数", "沪深300", "创业板指", "事实观察"], rows
    )
    aligned = align_intraday_events(minute_pack, events_pack)
    visible = []
    used_buckets = set()
    for item in aligned:
        if item["event_scope"] != "macro_policy" or item["max_abs_move_pct"] < 0.3:
            continue
        published = datetime.fromisoformat(item["published_at"])
        bucket = (published.hour, published.minute // 10)
        if bucket in used_buckets:
            continue
        used_buckets.add(bucket)
        visible.append(item)
        if len(visible) >= 3:
            break
    if visible:
        event_rows = []
        for x in visible:
            moves = x["moves_30m_pct"]
            title = html.escape(x["title"][:100])
            window = next(info for info in x["windows"].values() if info["complete"])
            interval = f'{cn_timestamp(window["actual_start"]):%H:%M}–{cn_timestamp(window["actual_end"]):%H:%M}'
            event_rows.append([
                datetime.fromisoformat(x["published_at"]).strftime("%H:%M"),
                title,
                interval,
                fmt_pct(moves.get("sh000001")),
                fmt_pct(moves.get("sh000300")),
                fmt_pct(moves.get("sz399006")),
                (fmt_num(x.get("volume_ratio"), 2) + "x")
                if x.get("volume_ratio") is not None else "未取得",
                x["alignment_strength"],
            ])
        driver = (
            '<h3>带时间戳候选事件与行情同步</h3>'
            '<p class="warning">下表只说明快讯发布时间与随后30分钟行情同步，'
            '非整5分钟发布时使用向下对齐的5分钟价格代理，实际观察区间见表；'
            '不代表已经证明价格因果；未取得事件真实发生时间或官方文件时，不升级为“已确认驱动”。</p>'
            '<div class="scroll">'
            + html_table(
                ["发布时间", "候选事件", "实际价格区间", "上证30分钟", "沪深300 30分钟",
                 "创业板30分钟", "成交量比", "同步强度"],
                event_rows,
            )
            + "</div>"
        )
    else:
        driver = (
            '<p class="note">完整有效窗口中，未发现达到阈值的30分钟同步价格反应；'
            '本节不指定单一核心驱动。</p>'
        )
    pending_count = sum(not any(info["complete"] for info in item["windows"].values()) for item in aligned)
    if pending_count:
        driver += f'<p class="note">{pending_count} 条事件窗口待完成或数据不足，未输出正式30分钟反应或同步评级。</p>'
    section = (
        '<section id="intraday"><h2>3. 盘中节奏与核心驱动</h2>'
        '<p class="note">盘中阶段由腾讯5分钟K重建；集合竞价阶段仅使用开盘价相对昨收，'
        '不根据日K猜测。[S1]</p>'
        f'<div class="scroll">{timeline}</div>{driver}</section>'
    )
    return section, aligned

def html_table(headers, rows):
    return _shared_html_table(headers, rows)


def market_snapshot(day_dir: Path, cutoff: datetime, mode: str = "intraday") -> dict:
    """Only reuse snapshots actually captured on the report day before cutoff.

    Legacy evidence is eligible by generated_at, never by its user-supplied as_of.
    A file containing later data relabelled as an earlier cutoff is not a snapshot.
    """
    paths = list((day_dir / "snapshots").glob("*.json"))
    paths += [day_dir / variant / "market_data" / "evidence.json"
              for variant in ("intraday_latest", "close")]
    eligible = []
    for path in paths:
        data = read_json_file(path, {})
        if not isinstance(data, dict):
            continue
        captured = cn_timestamp(data.get("captured_at") or data.get("generated_at"))
        # Collection may finish after generated_at (the run start). Account for
        # every declared observation boundary when replaying an evidence file.
        boundaries = [cn_timestamp(data.get(key)) for key in ("as_of", "snapshot_as_of")]
        if captured:
            captured = max([captured] + [stamp for stamp in boundaries if stamp])
        if (captured and captured.date() == cutoff.date() and captured <= cutoff
                and data.get("report_date") == cutoff.date().isoformat()
                and (mode != "close" or captured.hour >= 15)):
            # Old six-digit keys may already have overwritten another market.
            # Their identity cannot be recovered safely from price or name.
            old_quotes = data.get("eastmoney_quotes") or {}
            data["eastmoney_quotes"] = {
                key.lower(): quote for key, quote in old_quotes.items()
                if re.fullmatch(r"(?:sh|sz|bj)\d{6}", key.lower())
            } if isinstance(old_quotes, dict) else {}
            eligible.append((captured, {**data, "captured_at": captured.isoformat()}))
    return max(eligible, key=lambda item: item[0])[1] if eligible else {}


def quotes_at_cutoff(codes, live, saved, daily, minute_pack, cutoff, mode):
    """Accept timestamped quotes or reconstruct only fields justified by K lines."""
    out = {}
    day = cutoff.date().isoformat()
    for code in codes:
        for candidate in (live.get(code), saved.get(code)):
            if not candidate:
                continue
            observed = cn_timestamp(candidate.get("time"))
            if (observed and observed.date() == cutoff.date() and observed <= cutoff
                    and candidate.get("price") is not None
                    and (mode != "close" or observed >= cutoff.replace(hour=15, minute=0, second=0, microsecond=0))):
                out[code] = {**candidate, "time": observed.isoformat()}
                break
        if code in out:
            continue
        history = sorted((r for r in daily.get(code, []) if r["date"] < day), key=lambda r: r["date"])
        previous = history[-1]["close"] if history else None
        if mode == "close":
            rows = [r for r in daily.get(code, []) if r["date"] == day]
            if not rows:
                continue
            row = rows[-1]
            quote = {key: row.get(key) for key in ("open", "high", "low")}
            quote.update(price=row["close"], time=f"{day}T15:00:00+08:00",
                         source="Tencent unadjusted daily reconstruction")
        else:
            rows = sorted((r for r in (minute_pack.get("symbols") or {}).get(code, [])
                           if (stamp := cn_timestamp(r.get("time")))
                           and stamp.date() == cutoff.date() and stamp <= cutoff), key=lambda r: r["time"])
            if not rows:
                continue
            quote = {"price": rows[-1]["close"], "time": rows[-1]["time"],
                     "source": "Tencent completed 5m reconstruction"}
            # Session open/high/low need all completed bars, not a partial window.
            expected = {t.isoformat() for t in _expected_cn_bars(day, cn_timestamp(rows[-1]["time"]))}
            if expected and expected == {cn_timestamp(r["time"]).isoformat() for r in rows}:
                quote.update(open=rows[0].get("open"), high=max(r["high"] for r in rows),
                             low=min(r["low"] for r in rows))
        quote.update(name=code, last_close=previous,
                     change_pct=pct(quote["price"], previous), reconstructed=True)
        out[code] = quote
    return out


def events_at_cutoff(events, cutoff):
    # A merged RSS event can include later articles: use its latest publication.
    return [event for event in events if
            (stamp := cn_timestamp(event.get("last_updated_at") or event.get("published_at")))
            and stamp <= cutoff]


def us_reference(runs_root: Path, cutoff: datetime) -> dict:
    """Pick completed US daily observations, with explicit dates for stale data."""
    expected = latest_completed_us_session(cutoff)
    assets = {}
    for path in sorted((runs_root / "us").glob("*/market_data/evidence.json"), reverse=True):
        pack_day = path.parents[1].name
        try:
            pack_close = us_session_close(datetime.fromisoformat(pack_day).date())
        except ValueError:
            continue
        if pack_close is None or pack_day > expected:
            continue
        data = read_json_file(path, {})
        if not isinstance(data, dict) or data.get("report_date") != pack_day:
            continue
        generated = cn_timestamp(data.get("generated_at"))
        for symbol in ("SPY", "QQQ", "SMH"):
            block = data.get(symbol) or {}
            if not isinstance(block, dict):
                continue
            rows = []
            for row in block.get("rows") or []:
                if not isinstance(row, dict) or not finite_number(row.get("close")) or row["close"] <= 0:
                    continue
                try:
                    close = us_session_close(datetime.fromisoformat(row["date"]).date())
                except (ValueError, KeyError, TypeError):
                    continue
                if (close is not None and close <= cutoff and row["date"] <= pack_day
                        and (generated is None or close <= generated)):
                    rows.append(row)
            rows.sort(key=lambda row: row["date"])
            if not rows or (symbol in assets and rows[-1]["date"] <= assets[symbol]["date"]):
                continue
            last = rows[-1]
            previous_day = latest_completed_us_session(
                us_session_close(datetime.fromisoformat(last["date"]).date()) - timedelta(microseconds=1))
            previous = next((row for row in reversed(rows[:-1]) if row["date"] == previous_day), None)
            assets[symbol] = {"date": last["date"], "close": last["close"],
                              "change_pct": pct(last["close"], previous["close"]) if previous else None,
                              "stale": last["date"] != expected, "evidence_path": str(path)}
        if len(assets) == 3 and all(row["date"] == expected for row in assets.values()):
            break
    return {"expected_date": expected, "as_of": cutoff.isoformat(), "assets": assets}

def main(
    report_date: str | None = None,
    *,
    mode: str = "close",
    as_of: datetime | None = None,
    refresh: bool = False,
    cache_seconds: int = 120,
    template: Path | None = None,
    reports_dir: Path | None = None,
    runs_dir: Path | None = None,
):
    """Run the A-share report, with paths supplied by the package pipeline.

    All arguments remain optional so the compatibility script is still runnable.
    """
    global TEMPLATE, REPORTS, DATA_ROOT
    TEMPLATE = Path(template) if template else ROOT / "A股收盘日报_template.html"
    REPORTS = Path(reports_dir) if reports_dir else ROOT / "reports"
    DATA_ROOT = (Path(runs_dir) if runs_dir else ROOT / "runs") / "cn"
    now=datetime.now(SH_TZ)
    requested_cutoff = as_of
    as_of = (as_of or now).astimezone(SH_TZ)
    if mode not in {"intraday", "close"}:
        raise ValueError("A股 mode 必须是 intraday 或 close")
    # Index K-line is the source of truth for the last completed exchange session.
    idx_k=fetch_klines([s for s,_ in INDEXES[:1]], 320)
    candidate_dates=[r["date"] for r in idx_k.get("sh000001",[]) if r["date"] <= now.strftime("%Y-%m-%d")]
    if mode == "intraday":
        report_date = report_date or as_of.date().isoformat()
    else:
        report_date=report_date or (candidate_dates[-1] if candidate_dates else now.strftime("%Y-%m-%d"))
    report_day = datetime.strptime(report_date, "%Y-%m-%d").replace(tzinfo=SH_TZ)
    rolling_live = requested_cutoff is None and report_day.date() == now.date()
    if requested_cutoff is None and report_day.date() != now.date():
        if mode == "intraday":
            raise ValueError("历史盘中报告必须指定 --as-of HH:MM")
        as_of = report_day.replace(hour=23, minute=59, second=59)
    if as_of.date() != report_day.date():
        raise ValueError("截止时间必须属于报告日期")
    if as_of > now:
        raise ValueError("报告截止时间不能晚于当前时间")
    if mode == "close" and as_of < report_day.replace(hour=15):
        raise ValueError("收盘报告截止时间不能早于15:00")
    # Replay uses unadjusted history: later corporate actions must not rewrite
    # the reconstructed historical quote through today's forward adjustment.
    adjustment = "qfq" if rolling_live else ""
    variant = "intraday_latest" if mode == "intraday" else "close"
    run_dir = DATA_ROOT/report_date/variant
    news_dir=DATA_ROOT/report_date/"news"
    data_dir=run_dir/"market_data"
    data_dir.mkdir(parents=True,exist_ok=True); REPORTS.mkdir(exist_ok=True)
    saved = market_snapshot(DATA_ROOT / report_date, as_of, mode)
    codes=[s for s,_ in INDEXES]+POOL
    try: tq=tencent_quotes(codes)
    except Exception: tq={}
    minute_pack = collect_intraday_klines(
        report_date, data_dir, mode=mode, as_of=as_of, refresh=refresh,
        cache_seconds=cache_seconds,
    )
    timestamped_events_pack = collect_timestamped_events(
        report_date, data_dir, mode=mode, as_of=as_of, refresh=refresh,
        cache_seconds=cache_seconds,
    )
    # These endpoints expose current snapshots, not point-in-time history.
    if rolling_live:
        try: eq=em_quotes([x for x,_ in INDEXES]+POOL)
        except Exception: eq={}
        inds=industry_rows(); flow_ind=board_flow("industry","today"); flow_con=board_flow("concept","today")
        pool_results = [pool(endpoint, report_date) for endpoint in
                        ("getTopicZTPool", "getTopicZBPool", "getTopicDTPool", "getYesterdayZTPool")]
    else:
        eq = saved.get("eastmoney_quotes") or {}
        inds = saved.get("industries") or []
        flow_ind = saved.get("industry_flow") or []
        flow_con = saved.get("concept_flow") or []
        pool_results = saved.get("limit_pools") or [None] * 4
    snapshot_time = datetime.now(SH_TZ) if rolling_live else cn_timestamp(saved.get("captured_at"))
    kl = fetch_klines([s for s, _ in INDEXES] + POOL, 320, adjustment=adjustment)
    for s, _ in INDEXES:
        if not kl.get(s):
            kl[s] = idx_k.get(s, [])
    limit_pool_available = all(result is not None for result in pool_results)
    zt, zb, dt, yzt = [result or [] for result in pool_results]
    # Secondary pull for limit-up candidates shown in the正文 table.
    limit_codes=[x.get("c") for x in sorted(zt,key=lambda a:(a.get("lbc") or 0,a.get("fund") or 0),reverse=True)[:8] if x.get("c")]
    if limit_codes:
        try: tq.update(tencent_quotes(limit_codes))
        except Exception: pass
    extra_limit_codes = [c for c in limit_codes if c not in kl]
    if extra_limit_codes:
        kl.update(fetch_klines(extra_limit_codes, 80, adjustment=adjustment))
    if rolling_live:
        as_of = datetime.now(SH_TZ)
    tq = quotes_at_cutoff(list(dict.fromkeys(codes + limit_codes)), tq,
                          saved.get("quotes") or {}, kl, minute_pack, as_of, mode)
    # Do not let a previous day's close appear as today's intraday price.
    technical_date = (report_day - timedelta(days=1)).date().isoformat() if mode == "intraday" else report_date
    tech = {c: technical(kl.get(c, []), technical_date) for c in codes + limit_codes}
    if mode == "intraday":
        for c, values in tech.items():
            quote = tq.get(c) or {}
            values.update(close=quote.get("price"), day=quote.get("change_pct"),
                          vol_ratio=quote.get("vol_ratio"))
    else:
        tech = {c: values if values.get("last_date") == report_date else technical([], report_date)
                for c, values in tech.items()}
    lhb = ((dragon_tiger(report_date) if rolling_live else saved.get("dragon_tiger") or [])
           if mode == "close" else [])
    ranked_pool = sorted(
        [c for c in POOL if (tq.get(c) or {}).get("change_pct") is not None],
        key=lambda c: abs((tq.get(c) or {}).get("change_pct") or 0),
        reverse=True,
    )
    announcement_codes = list(dict.fromkeys(limit_codes + ranked_pool))[:10]
    announcements_pack=fetch_announcements(announcement_codes) if mode == "close" else {}
    if rolling_live:
        as_of = datetime.now(SH_TZ)
        snapshot_dir = DATA_ROOT / report_date / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot = {"report_date": report_date, "captured_at": as_of.isoformat(),
                    "quotes": tq, "eastmoney_quotes": eq, "industries": inds,
                    "industry_flow": flow_ind, "concept_flow": flow_con,
                    "limit_pools": pool_results, "dragon_tiger": lhb}
        (snapshot_dir / f"{as_of:%H%M%S_%f}.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    ev=events_at_cutoff(news_events(news_dir), as_of)
    announcements_pack = {code: events_at_cutoff(items, as_of)
                          for code, items in announcements_pack.items()}
    quality = intraday_quality(
        report_date, minute_pack, timestamped_events_pack, tq,
        mode=mode, as_of=as_of,
    )
    intraday_section, aligned_events = build_intraday_section(
        report_date, minute_pack, timestamped_events_pack, quality, tq,
        mode=mode, as_of=as_of,
    )
    alignment_pack = {
        "schema_version": "1.0",
        "report_date": report_date,
        "mode": mode,
        "as_of": as_of.isoformat(),
        "is_final": quality.get("is_final", False),
        "generated_at": now.isoformat(),
        "quality": quality,
        "alignments": aligned_events,
        "causality_note": "同步行情只表示时间对齐，不自动证明事件导致价格变化。",
    }
    (data_dir/"intraday_alignment.json").write_text(
        json.dumps(alignment_pack,ensure_ascii=False,indent=2)
    )
    evidence={"schema_version":"1.1","market":"cn","report_type":mode,"report_date":report_date,"as_of":as_of.isoformat(),"session_state":quality.get("session_state"),"is_final":quality.get("is_final",False),"generated_at":now.isoformat(),"quotes":tq,"eastmoney_quotes":eq,"technical":tech,"technical_cutoff":technical_date,"industries":inds,"industry_flow":flow_ind,"concept_flow":flow_con,"limit_pool_available":limit_pool_available,"limit_up_count":len(zt) if limit_pool_available else None,"break_count":len(zb) if limit_pool_available else None,"limit_down_count":len(dt) if limit_pool_available else None,"yesterday_limit_count":len(yzt) if limit_pool_available else None,"dragon_tiger":lhb[:50],"announcements":announcements_pack,"intraday_quality":quality,"intraday_outputs":{"minute_indices":"minute_indices.json","timestamped_events":"intraday_events.json","alignment":"intraday_alignment.json"}}
    evidence.update(snapshot_as_of=snapshot_time.isoformat() if snapshot_time else None,
                    daily_price_basis="unadjusted" if not adjustment else adjustment,
                    limit_pools=pool_results,
                    cutoff_policy="timestamped quotes; eligible snapshots; K-line reconstruction; otherwise missing")
    cross_market = us_reference(DATA_ROOT.parent, as_of)
    evidence["cross_market"] = cross_market
    assessment = cn_market_assessment(tq, tech, SECTOR_BASKETS, as_of, mode)
    evidence["market_assessment"] = assessment
    (data_dir/"evidence.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    return render(report_date,now,tq,eq,tech,kl,inds,flow_ind,flow_con,zt,zb,dt,yzt,lhb,ev,announcements_pack,news_dir,data_dir,intraday_section,quality,timestamped_events_pack,mode=mode,as_of=as_of,limit_pool_available=limit_pool_available,cross_market=cross_market,assessment=assessment)

def render(report_date,now,tq,eq,tech,kl,inds,flow_ind,flow_con,zt,zb,dt,yzt,lhb,ev,announcements_pack,news_dir,data_dir,intraday_section,quality,timestamped_events_pack,*,mode="close",as_of=None,limit_pool_available=True,cross_market=None,assessment=None):
    as_of = (as_of or now).astimezone(SH_TZ)
    is_intraday = mode == "intraday"
    price_label = "最新价" if is_intraday else "收盘"
    def q(symbol):
        raw=tq.get(symbol) or {}
        e=eq.get(market_symbol(symbol), {})
        z={**e,**{k:v for k,v in raw.items() if v is not None}}
        return z
    def ma20_state(snapshot):
        close, ma20 = snapshot.get("close"), snapshot.get("ma20")
        if close is None or ma20 is None:
            return "未取得"
        return "MA20上方" if close > ma20 else "MA20下方"
    idxrows=[]
    for s,n in INDEXES:
        z=q(s); t=tech.get(s,{})
        idxrows.append([f"<strong>{n}</strong><br><span class='muted'>{s[-6:]}</span>",fmt_num(z.get("price"),2),fmt_pct(z.get("change_pct")),fmt_num(z.get("high"),2)+" / "+fmt_num(z.get("low"),2),fmt_amount_wan(z.get("amount_wan")),fmt_pct(t.get("r5")),fmt_pct(t.get("r20")),ma20_state(t)])
    market_html=html_table(["标的",price_label,"涨跌","日内高/低","成交额","5日","20日","技术状态"],idxrows)
    assessment = assessment or cn_market_assessment(tq, tech, SECTOR_BASKETS, as_of, mode)
    sec = assessment["sectors"]
    status = assessment["status"] + ("（盘中，待收盘确认）" if is_intraday else "")
    focus = "、".join(assessment["focus"]) or "暂无符合条件的正收益领先篮子"
    if len(sec) < 2:
        sector_note = f'数据不足：合格篮子 {len(sec)}/{len(SECTOR_BASKETS)}，取消强弱排名和重点方向判断。'
    elif not assessment["sector_ranked"]:
        sector_note = "合格篮子收益相同，不按配置顺序选择强弱或重点方向。"
    else:
        sector_note = f'仅比较 {len(sec)}/{len(SECTOR_BASKETS)} 个合格代表股篮子，不代表全行业排名或资金流向。'
    sector_html=html_table(["排名","代表股篮子","当日平均","5日平均（收盘口径）","有效成分"],
                           [[x["rank"] if x["rank"] is not None else "不排名",html.escape(x["name"]),
                             fmt_pct(x["day"]),fmt_pct(x["r5"]),f'{x["n"]}/{x["total"]}'] for x in sec])
    sector_html += ('<p class="note">'+html.escape(sector_note)
                    +' 篮子有效门槛：至少2只且覆盖率≥50%；5日收益缺失不填零。[S1]</p>')
    summary_rows = [[label, html.escape(row["name"]), fmt_pct(row["day"]),
                     f'{row["n"]}/{row["total"]}', "代表股等权价格代理，非资金流"]
                    for label, group in (("相对强势", assessment["leaders"]), ("相对弱势", assessment["laggards"]))
                    for row in group]
    sector_summary = (html_table(["类型","代表股篮子","当日平均","有效成分","口径"], summary_rows)
                      if summary_rows else "") + '<p>'+html.escape(sector_note)+'</p>'
    sector_summary += '<p>重点观察：'+html.escape(focus)+'。</p>'
    core_values = [value for value in assessment["core_changes"].values() if value is not None]
    core_note = (f'有效核心报价 {len(core_values)}/4；上涨 {sum(v > 0 for v in core_values)}、'
                 f'下跌 {sum(v < 0 for v in core_values)}、持平 {sum(v == 0 for v in core_values)}。')
    core_note += ("数据不足，暂停市场方向判断。" if len(core_values) < 4
                  else "仅描述核心指数方向，不据此认定全市场普涨普跌、风险偏好或资金流向。")
    flow_note="已取得板块主力净流入字段" if flow_ind else "行业资金流接口未取得，以下使用代表股价格/成交量代理"
    if flow_ind:
        sector_html=html_table(["排名","行业","当日涨跌","主力净流入","主力净占比","领涨股"],[[i+1,x.get("name"),fmt_pct(x.get("change")),fmt_money_yi(x.get("main_net")),fmt_pct(x.get("main_pct")),html.escape(str(x.get("leader") or "未取得"))] for i,x in enumerate(flow_ind[:15])])
    style=[]
    for s,n in [("sh000300","大盘权重"),("sh000905","中盘"),("sz399852","中小盘"),("sz399006","创业成长"),("sh000688","科创成长")]: style.append([n,fmt_pct(tech.get(s,{}).get("day")),fmt_pct(tech.get(s,{}).get("r5"))])
    style_html=html_table(["风格","当日","5日"],style)
    themes="<ul>"+"".join(f"<li>{html.escape(e.get('headline',''))} <span class='tag'>{html.escape(str(e.get('topic','')))}</span> <span class='muted'>[S4]</span></li>" for e in ev[:8])+"</ul>"
    # Breadth: direct limit-pool counts plus index constituent counts where available.
    limit_pool_value = (lambda value: value if limit_pool_available else "未取得")
    breadth_rows=[["上证指数成分上涨/下跌",f"{eq.get('sh000001',{}).get('up_count','未取得')} / {eq.get('sh000001',{}).get('down_count','未取得')}","指数成分统计代理"],["沪深300成分上涨/下跌",f"{eq.get('sh000300',{}).get('up_count','未取得')} / {eq.get('sh000300',{}).get('down_count','未取得')}","指数成分统计"],["涨停家数",limit_pool_value(len(zt)),"打板池"],["炸板家数",limit_pool_value(len(zb)),"打板池"],["跌停家数",limit_pool_value(len(dt)),"打板池"],["昨日涨停池",limit_pool_value(len(yzt)),"昨日涨停跟踪"]]
    breadth_html=html_table(["指标","报告日","口径"],breadth_rows)
    break_denominator = len(zt) + len(zb)
    break_rate = f"{len(zb) / break_denominator * 100:.1f}%" if break_denominator else "未取得（无涨停或炸板样本）"
    breadth_comment=(f'<p class="note">涨停{len(zt)}家、炸板{len(zb)}家、跌停{len(dt)}家；炸板率={break_rate}。全市场直接涨跌家数未取得，未用指数成分统计冒充全市场。</p>' if limit_pool_available else '<p class="note">涨停、炸板、跌停及昨日涨停池接口未取得，本节不将缺失写为零。</p>')
    pool_map={x.get("c"):x for x in zt+zb+dt}
    movers=[]
    # dual channel: limit pool first, then fixed pool by absolute movement
    selected=[]
    for x in sorted(zt,key=lambda a:(a.get("lbc") or 0,a.get("fund") or 0),reverse=True)[:8]: selected.append(x.get("c"))
    rest=sorted([c for c in POOL if q(c).get("change_pct") is not None],key=lambda c:abs(q(c).get("change_pct") or 0),reverse=True)
    for c in rest:
        if c not in selected: selected.append(c)
    for c in selected[:15]:
        z=q(c); t=tech.get(c,{}) ; p=pool_map.get(c,{})
        name=z.get("name") or c; reason=p.get("hybk") or "固定观察池/全市场异动"
        movers.append([f"<strong>{html.escape(str(name))}</strong><br><span class='muted'>{c}</span>",fmt_pct(z.get("change_pct")),fmt_amount_wan(z.get("amount_wan")),fmt_num(t.get("vol_ratio"),2)+"x",fmt_num(z.get("turnover_pct"),2)+"%",html.escape(str(reason)),"涨停/异动池" if c in pool_map else "固定池", "深入研究候选" if c in selected[:5] else "等待证据"])
    movers_html=html_table(["股票","涨跌","成交额","量比","换手率","板块/原因","信号","研究状态"],movers)
    news_top=ev[:10]
    macro=[e for e in ev if e.get("topic") in ("macro","policy","energy")][:6]
    macro_html="<ul>"+"".join(f"<li>{html.escape(e.get('headline',''))} <span class='muted'>[S4]</span></li>" for e in macro)+"</ul>" if macro else '<p class="muted">未取得明确宏观事件。</p>'
    index_amounts = [q(symbol).get("amount_wan") for symbol in ("sh000001", "sz399001")]
    market_turnover = sum(index_amounts) * 1e4 if all(value is not None for value in index_amounts) else None
    macro_table=html_table(["变量","最新值","口径"],[["人民币/资金面","未取得","本次未补充可靠官方序列"],["全市场成交额",fmt_money_yi(market_turnover),"上证+深证指数成交额合计代理"],["涨停/炸板",("%d / %d"%(len(zt),len(zb))) if limit_pool_available else "未取得","打板池"]])
    cross_market = cross_market or {"expected_date": latest_completed_us_session(as_of), "assets": {}}
    cross_rows=[]
    for s,n in [("SPY","标普500"),("QQQ","纳指100"),("SMH","半导体ETF")]:
        observation = cross_market["assets"].get(s) or {}
        label = ("过期参考" if observation.get("stale") else "最近完整交易日") if observation else "未取得"
        cross_rows.append([n,fmt_num(observation.get("close")),fmt_pct(observation.get("change_pct")),
                           observation.get("date", "未取得"), label + " [S6]"])
    cross_html = ('<p class="note">截至报告时间，最近已完成美股交易日：'
                  + cross_market["expected_date"] + ' ET；更早数据明确标记为过期参考。</p>'
                  + html_table(["资产","收盘值","日变动","实际数据日期（ET）","口径"],cross_rows))
    tech_rows=[]
    for s,n in INDEXES[:7]:
        z=tech.get(s,{})
        tech_rows.append([n,fmt_num(z.get("close")),fmt_num(z.get("ma20")),fmt_num(z.get("ma50")),fmt_num(z.get("ma200")),fmt_num(z.get("rsi"),1),z.get("macd") or "未取得",fmt_num(z.get("vol_ratio"),2)+"x",ma20_state(z)])
    technical_html=html_table(["标的",price_label,"MA20","MA50","MA200","RSI14","MACD","量比","趋势"],tech_rows)
    lhb_rows=[[x.get("code"),html.escape(str(x.get("name"))),html.escape(str(x.get("reason") or "未取得"))[:80],fmt_money_yi((x.get("net") or 0)*1e4),fmt_pct(x.get("change")),fmt_num(x.get("turnover"),2)+"%"] for x in lhb[:12]]
    disclosure_html=html_table(["代码","公司","上榜/披露原因","净买入","涨跌","换手率"],lhb_rows) if lhb_rows else '<p class="muted">未取得报告日龙虎榜。</p>'
    future_html=html_table(["日期","公司","事件类型","已确认日期/窗口","关注点"],[["未来3个交易日","未取得","预约披露","未取得","以交易所/公司公告为准"],["收盘后","见新闻候选","公开新闻","已抓取原始RSS","需回到原始公告核验"]])
    news_manifest = read_json_file(news_dir/"manifest.json", {}) or {}
    news_window = news_manifest.get("window") or {}
    news_stats = news_manifest.get("stats") or {}
    news_desc = (
        f"{news_stats.get('selected_sources','未取得')}源、窗口 "
        f"{news_window.get('start','未取得')}–{news_window.get('end','未取得')}"
    )
    intraday_event_count = quality.get("a_share_relevant_event_count", 0)
    reference_paths = sorted({row["evidence_path"] for row in cross_market["assets"].values()})
    source_note = ("[S6] 配置运行目录中的美股日线证据；最近已完成交易日 "
                   + cross_market["expected_date"] + " ET；实际日期及过期状态见跨市场表。证据文件："
                   + html.escape("；".join(reference_paths) or "未取得") + "。")
    sources='<ol><li>[S1] 腾讯财经行情与K线（HTTP）：指数、个股行情、日K及5分钟K、成交量、成交额和换手率；分钟数据缓存于 '+html.escape(str(data_dir/"minute_indices.json"))+'；as_of '+html.escape(as_of.strftime('%Y-%m-%d %H:%M Asia/Shanghai'))+'。</li><li>[S2] 东财 push2/ulist：指数成分涨跌统计；板块排名接口失败时不将空结果当作数据。</li><li>[S3] 东财 push2ex：涨停、炸板、跌停、昨日涨停池；交易日 '+report_date+'。</li><li>[S4] scan-market-news CN证据包：'+html.escape(str(news_dir))+'；'+html.escape(news_desc)+'。</li><li>[S5] 东财 datacenter：报告日龙虎榜；仅收盘模式获取。</li><li>'+source_note+'</li><li>[S7] 财联社电报：报告日交易时段带发布时间快讯，A股相关候选 '+str(intraday_event_count)+' 条；发布时间不自动等于事件发生时间，时间对齐不自动证明价格因果。</li><li>[S8] 巨潮资讯：重点异动股票公告及披露时间；仅收盘模式补充重点公告。</li></ol>'
    q0=q("sh000001"); q3=q("sh000300"); qg=q("sz399006"); sz=q("sz399001")
    turnover = market_turnover / 1e4 if market_turnover is not None else None
    values={
      "REPORT_AS_OF_ISO":as_of.isoformat(),
      "REPORT_DATE_SHANGHAI":report_date,"MARKET_PHASE":"A股盘中快报" if is_intraday else "A股收盘复盘","GENERATED_AT_SHANGHAI":now.strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai"),"DATA_AS_OF":as_of.strftime("%Y-%m-%d %H:%M Asia/Shanghai"),"SESSION_SCOPE":"盘中快照，数据未经收盘确认" if is_intraday else "集合竞价、上午盘、下午盘；收盘后公告单独标注",
      "SSE_CLOSE":fmt_num(q0.get("price")),"SSE_CHANGE":fmt_pct(q0.get("change_pct")),"CSI300_CLOSE":fmt_num(q3.get("price")),"CSI300_CHANGE":fmt_pct(q3.get("change_pct")),"CHINEXT_CLOSE":fmt_num(qg.get("price")),"CHINEXT_CHANGE":fmt_pct(qg.get("change_pct")),"TOTAL_TURNOVER":fmt_amount_wan(turnover),"TURNOVER_CHANGE":f'<span class="muted">成交额变化：未取得同口径前值</span>',
      "FIRST_READ_HTML":''.join(f'<div class="decision"><small>{a}</small><strong>{b}</strong></div>' for a,b in [("核心变化",status),("主线",html.escape(focus)),("已反映/待验证","价格先行，公告与资金需验证"),("下一验证点","成交额、宽度与涨停梯队")]),
      "EXECUTIVE_SUMMARY_HTML":f'<p class="lead">{report_date} A股{"截至 "+as_of.strftime("%H:%M") if is_intraday else "收盘"}：上证指数 {fmt_pct(q0.get("change_pct"))}，沪深300 {fmt_pct(q3.get("change_pct"))}，创业板指 {fmt_pct(qg.get("change_pct"))}；成交额以沪深指数成交额合计为代理。{"涨停"+str(len(zt))+"家、炸板"+str(len(zb))+"家、跌停"+str(len(dt))+"家。" if limit_pool_available else "涨跌停池未取得。"}{"以上均为盘中快照，未经收盘确认。" if is_intraday else ""}[S1][S3]</p>',"MARKET_STATUS":status,
      "MARKET_OVERVIEW_TABLE_HTML":market_html,"RISK_APPETITE_HTML":f'<p class="note">以沪深300、中证1000、创业板指、涨跌停与炸板率联合判断风险偏好；全市场直接涨跌家数本次未取得。[S1][S3]</p>',
      "INTRADAY_SECTION_HTML":intraday_section,
      "MACRO_LIQUIDITY_TABLE_HTML":macro_table,"CROSS_ASSET_TABLE_HTML":cross_html,"MACRO_EVENTS_HTML":macro_html,
      "INDUSTRY_ROTATION_TABLE_HTML":sector_html,"THEME_ROTATION_HTML":themes,"STYLE_ROTATION_HTML":style_html,"SECTOR_TRANSMISSION_HTML":f'<p class="note">{flow_note}；如无明确板块资金字段，不使用“资金流入/流出”作为事实表述。</p>',
      "BREADTH_TABLE_HTML":breadth_html,"BREADTH_COMMENTARY_HTML":breadth_comment,"TECHNICAL_TABLE_HTML":technical_html,"TECHNICAL_COMMENTARY_HTML":('<p>均线、RSI和区间收益截至前一完整交易日；最新价与盘中量比来自实时行情，均为临时状态。[S1]</p>' if is_intraday else '<p>核心指数日K至少取260个有效交易日，MA200不足时显示“未取得”；量比为报告日成交量除以前20个交易日平均成交量。[S1]</p>'),
      "IDEA_FUNNEL_HTML":'<p class="note">异动候选采用固定观察池＋涨停/炸板/跌停池双通道；正文最多展示15只，原因缺失时不补写单一催化剂。</p>',"MOVERS_TABLE_HTML":movers_html,
      "DISCLOSURE_RECAP_TABLE_HTML":disclosure_html,"EVENT_CALENDAR_TABLE_HTML":future_html,"EARNINGS_ANALYSIS_HTML":'<p>本版未取得可统一核验的未来3个交易日预约披露明细；不将媒体标题写成正式业绩事实。</p>',
      "POSITIONING_SIGNALS_HTML":f'<p>报告日龙虎榜记录 {len(lhb)} 条；融资融券、股东户数和大宗交易本次未做全市场横截面补充，避免把单项数据当作全市场资金结论。[S5]</p>',"EVENT_ANALYSIS_HTML":'<p>重大事件只保留原始新闻候选，需以公司公告、交易所文件或监管材料二次核验。[S4]</p>',
      "UZI_CANDIDATES_HTML":'<p><strong>重点观察：</strong>'+"、".join(f"{q(c).get('name') or c}（{c}）" for c in selected[:5])+"。</p>","GLOBAL_LINKAGE_HTML":'<p>跨市场参考按各资产实际美股交易日期展示。过期数据仅供历史背景参考；未取得最近完整交易日数据时，不据此认定当日联动。[S6]</p>',
      "SCENARIO_TABLE_HTML":html_table(["情景","核心假设","触发条件","指数确认","宽度确认","失效条件","观察倾向"],[["偏强","成长风格止跌","创业板指收复MA20","沪深300不再创新低","涨停增加且炸板率下降","成交额继续萎缩","观察已取得数据的领先篮子"],["震荡/基准","板块轮动延续","指数在前收附近震荡","中证500相对稳定","涨跌停分化","权重与成长同步下破","等待主线确认"],["偏弱","风险偏好继续下降","沪深300与创业板同步走弱","成交额放大下跌","跌停增加、昨日涨停溢价转负","出现政策/公告反转","降低事件暴露"]]),"NEXT_SESSION_WATCHLIST_HTML":'<ul><li>成交额是否放大并得到上涨家数确认。</li><li>涨停梯队、炸板率和昨日涨停表现是否改善。</li><li>重点观察篮子能否延续相对表现；缺数据时先补齐证据。</li></ul>',
      "FINAL_CONCLUSION_HTML":f'<p class="lead">市场状态：<strong>{status}</strong>。{core_note}[S1]</p>',
      "SECTOR_SUMMARY_HTML":sector_summary,"MARKET_STAGE":status,
      "POSITIONING_BIAS_HTML":'<p>重点观察：'+html.escape(focus)+'。不将样本排名直接转化为交易或仓位建议。</p>',
      "VALIDATION_SIGNALS_HTML":'<ol><li>核验四个核心指数同日数据是否齐全。</li><li>观察指数方向是否得到全市场宽度和成交额确认。</li><li>比较合格篮子下一时段相对表现，不以缺失值排序。</li><li>新闻催化需以公告和价格反应分别核验。</li></ol>',
      "EVIDENCE_LIMITATIONS_HTML":('<ul><li>本页为盘中快照，价格、成交额、涨跌停池和板块排名仍可能变化。</li><li>均线与RSI以此前完整交易日为基础；龙虎榜和收盘后公告未纳入。</li><li>财联社和RSS时间戳主要是发布时间，时间对齐不证明价格因果。</li></ul>' if is_intraday else '<ul><li>东财板块排名/资金流接口本次出现连接风控时，行业表现使用代表股等权代理。</li><li>全市场直接涨跌家数、融资融券横截面和预约披露日历未完整取得。</li><li>盘中阶段已使用5分钟K重建；财联社和RSS时间戳仍主要是发布时间，未取得事件真实发生时间或官方文件时不称为已确认驱动。</li></ul>'),"SOURCES_HTML":sources,
    }
    values["EVIDENCE_LIMITATIONS_HTML"] += (
        '<p class="note">所有行情按报告日期与截止时间筛选。历史回放使用不复权日K重建收盘价，'
        '盘中可用已完成5分钟K重建价格；成交额、换手率、盘中量比、资金流及涨跌停池'
        '缺少截止前快照时显示“未取得”。各报价实际观测时间与重建来源见 evidence.json。</p>'
    )
    out=(REPORTS/f"A股盘中快报_{report_date}_{as_of:%H%M}.html" if is_intraday
         else REPORTS/f"A股收盘日报_{report_date}_Asia-Shanghai.html")
    render_template(TEMPLATE, values, out, strict=True)
    if is_intraday:
        render_template(TEMPLATE, values, REPORTS/"A股盘中快报_latest.html", strict=True)
    print(f"Wrote {out}")
    return out

def run(context):
    cache_seconds = int(
        context.config.get("markets", {}).get("cn", {}).get("intraday_cache_seconds", 120)
    )
    report_path = Path(main(
        context.report_date,
        mode=context.mode,
        as_of=context.as_of if context.as_of_explicit else None,
        refresh=context.refresh,
        cache_seconds=cache_seconds,
        template=context.template,
        reports_dir=context.reports_dir,
        runs_dir=context.runs_dir,
    ))
    match = re.search(r"(\d{4}-\d{2}-\d{2})", report_path.name)
    if not match:
        raise RuntimeError(f"无法从输出文件名识别报告日：{report_path.name}")
    return match.group(1), report_path


if __name__ == "__main__": main()
