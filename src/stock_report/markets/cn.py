#!/usr/bin/env python3
"""Generate the latest A-share close report with public, auditable endpoints."""
from __future__ import annotations

import hashlib, html, json, re, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from stock_report.metrics import technical_snapshot as _technical_snapshot
except ModuleNotFoundError:  # defensive fallback for standalone module execution
    _technical_snapshot = None
from stock_report.news import load_news_events
from stock_report.render import html_table as _shared_html_table, render_template

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
INTRADAY_INDEXES = [
    "sh000001", "sh000300", "sh000905",
    "sz399852", "sz399006", "sh000688",
]
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

def fmt_num(x, digits=2):
    if x is None: return "未取得"
    return f"{x:,.{digits}f}"

def fmt_pct(x, digits=2):
    if x is None: return '<span class="muted">未取得</span>'
    cls = "up" if x > 0 else "down" if x < 0 else "muted"
    return f'<span class="{cls}">{x:+.{digits}f}%</span>'

def fmt_money_yi(x):
    return "未取得" if x is None else f"{x/1e8:,.2f}亿"

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
    secids = []
    for raw in codes:
        c = raw.lower().replace("sh", "").replace("sz", "").replace("bj", "")
        # Explicit index prefixes are important for 000xxx ambiguity.
        p = raw[:2].lower() if raw[:2].lower() in ("sh", "sz", "bj") else code_prefix(c)
        secids.append(("1" if p == "sh" else "0") + "." + c)
    params = {"fltt": "2", "invt": "2", "fields": "f2,f3,f4,f12,f14,f104,f105",
             "secids": ",".join(secids)}
    d = get_json("https://push2.eastmoney.com/api/qt/ulist.np/get", params=params,
                 headers={"Referer": "https://quote.eastmoney.com/"}, timeout=15)
    diff = ((d.get("data") or {}).get("diff") or [])
    if isinstance(diff, dict): diff = list(diff.values())
    out = {}
    for x in diff:
        c = x.get("f12", "")
        out[c] = {"name": x.get("f14", ""), "price": x.get("f2"), "change_pct": x.get("f3"),
                  "change_amt": x.get("f4"), "up_count": x.get("f104"), "down_count": x.get("f105")}
    return out

def kline(symbol: str, n: int = 320) -> list[dict]:
    p = symbol.lower() if symbol[:2].lower() in ("sh", "sz", "bj") else code_prefix(symbol) + symbol
    try:
        d = get_json(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={p},day,,,{n},qfq", timeout=20)
        block = (d.get("data") or {}).get(p) or {}
        # 指数返回 day，个股前复权返回 qfqday。
        raw = block.get("day") or block.get("qfqday") or []
        rows=[]
        for x in raw:
            if len(x) < 6: continue
            try: rows.append({"date": x[0], "open": float(x[1]), "close": float(x[2]), "high": float(x[3]), "low": float(x[4]), "volume": float(x[5])})
            except Exception: pass
        return rows
    except Exception: return []

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

def collect_intraday_klines(report_date: str, data_dir: Path) -> dict:
    cache = data_dir / "minute_indices.json"
    old = read_json_file(cache, {})
    if old.get("report_date") == report_date and old.get("symbols"):
        return old
    symbols = {}
    for symbol in INTRADAY_INDEXES:
        rows = [x for x in minute_kline(symbol) if x["time"][:10] == report_date]
        symbols[symbol] = rows
    pack = {
        "schema_version": "1.0",
        "report_date": report_date,
        "interval": "5m",
        "timezone": "Asia/Shanghai",
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

def collect_timestamped_events(report_date: str, data_dir: Path, max_pages: int = 16) -> dict:
    """获取报告日财联社盘中快讯。快讯时间是候选证据，不自动等于事件发生时间。"""
    cache = data_dir / "intraday_events.json"
    old = read_json_file(cache, {})
    if old.get("report_date") == report_date and old.get("events"):
        return old
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
            if published.strftime("%Y-%m-%d") == report_date and in_session:
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
        return ((d.get("data") or {}).get("pool") or [])
    except Exception:return []

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
        d=get_json("https://www.cninfo.com.cn/new/hisAnnouncement/query",data=body,headers={"Content-Type":"application/x-www-form-urlencoded","Referer":"https://www.cninfo.com.cn/new/disclosure"},timeout=15)
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

def intraday_quality(report_date: str, minute_pack: dict, events_pack: dict,
                     quotes: dict) -> dict:
    checks = {}
    valid = 0
    for symbol in INTRADAY_INDEXES:
        rows = _rows_for_symbol(minute_pack, symbol)
        first = rows[0]["minute"] if rows else ""
        last = rows[-1]["minute"] if rows else ""
        quote_close = (quotes.get(symbol) or {}).get("price")
        minute_close = rows[-1]["close"] if rows else None
        close_error = abs(pct(minute_close, quote_close) or 0) if minute_close and quote_close else None
        ok = (
            len(rows) >= 48
            and first <= "09:35"
            and last >= "15:00"
            and close_error is not None
            and close_error <= 0.2
        )
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
    ready = coverage >= 0.8 and len(relevant_events) >= 3
    return {
        "ready": ready,
        "core_index_coverage": coverage,
        "valid_index_count": valid,
        "required_index_count": len(INTRADAY_INDEXES),
        "timestamped_event_count": len(events_pack.get("events") or []),
        "a_share_relevant_event_count": len(relevant_events),
        "checks": checks,
        "rules": {
            "minimum_bars_per_index": 48,
            "required_session_start": "09:35",
            "required_session_end": "15:00",
            "maximum_close_error_pct": 0.2,
            "minimum_core_index_coverage": 0.8,
            "minimum_relevant_timestamped_events": 3,
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

def align_intraday_events(minute_pack: dict, events_pack: dict) -> list[dict]:
    """衡量快讯发布后30分钟的同步行情；结果只表示时间对齐，不表示因果。"""
    aligned = []
    for event in events_pack.get("events") or []:
        if not event.get("a_share_relevant"):
            continue
        try:
            published = datetime.fromisoformat(event["published_at"])
        except Exception:
            continue
        end = published + timedelta(minutes=30)
        moves = {}
        for symbol in ("sh000001", "sh000300", "sz399006", "sh000688"):
            rows = _rows_for_symbol(minute_pack, symbol)
            base = _row_at_or_before(rows, published)
            after = _row_at_or_before(rows, end)
            moves[symbol] = pct(after["close"], base["close"]) if base and after else None
        sse_rows = _rows_for_symbol(minute_pack, "sh000001")
        event_idx = next(
            (i for i, x in enumerate(sse_rows) if datetime.fromisoformat(x["time"]) >= published),
            None,
        )
        volume_ratio = None
        if event_idx is not None and event_idx >= 6:
            before = sse_rows[event_idx - 6:event_idx]
            after_rows = sse_rows[event_idx:min(event_idx + 6, len(sse_rows))]
            before_avg = sum(x["volume_lots"] for x in before) / len(before) if before else 0
            after_avg = sum(x["volume_lots"] for x in after_rows) / len(after_rows) if after_rows else 0
            volume_ratio = after_avg / before_avg if before_avg else None
        max_move = max((abs(x) for x in moves.values() if x is not None), default=0)
        if max_move >= 0.5 and (volume_ratio or 0) >= 1.2:
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
                           quality: dict, quotes: dict) -> tuple[str, list[dict]]:
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
            event_rows.append([
                datetime.fromisoformat(x["published_at"]).strftime("%H:%M"),
                title,
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
            '不代表已经证明价格因果；未取得事件真实发生时间或官方文件时，不升级为“已确认驱动”。</p>'
            '<div class="scroll">'
            + html_table(
                ["发布时间", "候选事件", "上证30分钟", "沪深300 30分钟",
                 "创业板30分钟", "成交量比", "同步强度"],
                event_rows,
            )
            + "</div>"
        )
    else:
        driver = (
            '<p class="note">已取得盘中快讯时间戳，但未发现达到阈值的30分钟同步价格反应；'
            '本节不指定单一核心驱动。</p>'
        )
    section = (
        '<section id="intraday"><h2>3. 盘中节奏与核心驱动</h2>'
        '<p class="note">盘中阶段由腾讯5分钟K重建；集合竞价阶段仅使用开盘价相对昨收，'
        '不根据日K猜测。[S1]</p>'
        f'<div class="scroll">{timeline}</div>{driver}</section>'
    )
    return section, aligned

def html_table(headers, rows):
    return _shared_html_table(headers, rows)

def main(
    report_date: str | None = None,
    *,
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
    # Index K-line is the source of truth for the last completed exchange session.
    idx_k={s:kline(s,320) for s,_ in INDEXES[:1]}
    candidate_dates=[r["date"] for r in idx_k.get("sh000001",[]) if r["date"] <= now.strftime("%Y-%m-%d")]
    report_date=report_date or (candidate_dates[-1] if candidate_dates else now.strftime("%Y-%m-%d"))
    news_dir=DATA_ROOT/report_date/"news"; data_dir=DATA_ROOT/report_date/"market_data"; data_dir.mkdir(parents=True,exist_ok=True); REPORTS.mkdir(exist_ok=True)
    codes=[s for s,_ in INDEXES]+POOL
    try: tq=tencent_quotes(codes)
    except Exception: tq={}
    minute_pack = collect_intraday_klines(report_date, data_dir)
    timestamped_events_pack = collect_timestamped_events(report_date, data_dir)
    # Preserve explicit sh/sz prefixes for ambiguous 000xxx index codes.
    try: eq=em_quotes([x for x,_ in INDEXES]+POOL)
    except Exception: eq={}
    kl={}
    for s,_ in INDEXES:
        kl[s]=idx_k.get(s) or kline(s,320)
    for c in POOL: kl[c]=kline(c,320)
    tech={c:technical(kl.get(c,[]),report_date) for c in codes}
    inds=industry_rows(); flow_ind=board_flow("industry","today"); flow_con=board_flow("concept","today")
    zt=pool("getTopicZTPool",report_date); zb=pool("getTopicZBPool",report_date); dt=pool("getTopicDTPool",report_date); yzt=pool("getYesterdayZTPool",report_date)
    # Secondary pull for limit-up candidates shown in the正文 table.
    limit_codes=[x.get("c") for x in sorted(zt,key=lambda a:(a.get("lbc") or 0,a.get("fund") or 0),reverse=True)[:8] if x.get("c")]
    if limit_codes:
        try: tq.update(tencent_quotes(limit_codes))
        except Exception: pass
    for c in limit_codes:
        if c not in kl: kl[c]=kline(c,80)
        tech[c]=technical(kl.get(c,[]),report_date)
    lhb=dragon_tiger(report_date)
    ev=news_events(news_dir)
    ranked_pool = sorted(
        [c for c in POOL if (tq.get(c) or {}).get("change_pct") is not None],
        key=lambda c: abs((tq.get(c) or {}).get("change_pct") or 0),
        reverse=True,
    )
    announcement_codes = list(dict.fromkeys(limit_codes + ranked_pool))[:10]
    announcements_pack={c:announcements(c) for c in announcement_codes}
    quality = intraday_quality(report_date, minute_pack, timestamped_events_pack, tq)
    intraday_section, aligned_events = build_intraday_section(
        report_date, minute_pack, timestamped_events_pack, quality, tq
    )
    alignment_pack = {
        "schema_version": "1.0",
        "report_date": report_date,
        "generated_at": now.isoformat(),
        "quality": quality,
        "alignments": aligned_events,
        "causality_note": "同步行情只表示时间对齐，不自动证明事件导致价格变化。",
    }
    (data_dir/"intraday_alignment.json").write_text(
        json.dumps(alignment_pack,ensure_ascii=False,indent=2)
    )
    evidence={"report_date":report_date,"generated_at":now.isoformat(),"quotes":tq,"eastmoney_quotes":eq,"technical":tech,"industries":inds,"industry_flow":flow_ind,"concept_flow":flow_con,"limit_up_count":len(zt),"break_count":len(zb),"limit_down_count":len(dt),"yesterday_limit_count":len(yzt),"dragon_tiger":lhb[:50],"announcements":announcements_pack,"intraday_quality":quality,"intraday_outputs":{"minute_indices":"minute_indices.json","timestamped_events":"intraday_events.json","alignment":"intraday_alignment.json"}}
    (data_dir/"evidence.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    return render(report_date,now,tq,eq,tech,kl,inds,flow_ind,flow_con,zt,zb,dt,yzt,lhb,ev,announcements_pack,news_dir,data_dir,intraday_section,quality,timestamped_events_pack)

def render(report_date,now,tq,eq,tech,kl,inds,flow_ind,flow_con,zt,zb,dt,yzt,lhb,ev,announcements_pack,news_dir,data_dir,intraday_section,quality,timestamped_events_pack):
    def q(symbol):
        raw=tq.get(symbol) or {}
        e=eq.get(symbol.replace("sh","").replace("sz","").replace("bj","") ,{})
        z={**e,**{k:v for k,v in raw.items() if v is not None}}
        return z
    idxrows=[]
    for s,n in INDEXES:
        z=q(s); t=tech.get(s,{})
        idxrows.append([f"<strong>{n}</strong><br><span class='muted'>{s[-6:]}</span>",fmt_num(z.get("price"),2),fmt_pct(z.get("change_pct")),fmt_num(z.get("high"),2)+" / "+fmt_num(z.get("low"),2),fmt_money_yi((z.get("amount_wan") or 0)*1e4),fmt_pct(t.get("r5")),fmt_pct(t.get("r20")),"MA20上方" if t.get("close") and t.get("ma20") and t["close"]>t["ma20"] else "MA20下方"])
    market_html=html_table(["标的","收盘","涨跌","日内高/低","成交额","5日","20日","技术状态"],idxrows)
    # Representative sector baskets, used when board index endpoint is unavailable.
    sec=[]
    for name,members in SECTOR_BASKETS.items():
        vals=[tech.get(c,{}) for c in members if tech.get(c,{}).get("day") is not None]
        sec.append({"name":name,"day":sum(v["day"] for v in vals)/len(vals) if vals else None,"r5":sum(v.get("r5") or 0 for v in vals)/len(vals) if vals else None,"n":len(vals)})
    sec.sort(key=lambda x:x["day"] if x["day"] is not None else -999,reverse=True)
    sector_html=html_table(["排名","代表股篮子","当日平均","5日平均","口径"],[[i+1,x["name"],fmt_pct(x["day"]),fmt_pct(x["r5"]),"代表股等权代理"] for i,x in enumerate(sec)])
    flow_note="已取得板块主力净流入字段" if flow_ind else "行业资金流接口未取得，以下使用代表股价格/成交量代理"
    if flow_ind:
        sector_html=html_table(["排名","行业","当日涨跌","主力净流入","主力净占比","领涨股"],[[i+1,x.get("name"),fmt_pct(x.get("change")),fmt_money_yi(x.get("main_net")),fmt_pct(x.get("main_pct")),html.escape(str(x.get("leader") or "未取得"))] for i,x in enumerate(flow_ind[:15])])
    style=[]
    for s,n in [("sh000300","大盘权重"),("sh000905","中盘"),("sz399852","中小盘"),("sz399006","创业成长"),("sh000688","科创成长")]: style.append([n,fmt_pct(tech.get(s,{}).get("day")),fmt_pct(tech.get(s,{}).get("r5"))])
    style_html=html_table(["风格","当日","5日"],style)
    themes="<ul>"+"".join(f"<li>{html.escape(e.get('headline',''))} <span class='tag'>{html.escape(str(e.get('topic','')))}</span> <span class='muted'>[S4]</span></li>" for e in ev[:8])+"</ul>"
    # Breadth: direct limit-pool counts plus index constituent counts where available.
    breadth_rows=[["上证指数成分上涨/下跌",f"{eq.get('000001',{}).get('up_count','未取得')} / {eq.get('000001',{}).get('down_count','未取得')}","指数成分统计代理"],["沪深300成分上涨/下跌",f"{eq.get('000300',{}).get('up_count','未取得')} / {eq.get('000300',{}).get('down_count','未取得')}","指数成分统计"],["涨停家数",len(zt),"打板池"],["炸板家数",len(zb),"打板池"],["跌停家数",len(dt),"打板池"],["昨日涨停池",len(yzt),"昨日涨停跟踪"]]
    breadth_html=html_table(["指标","报告日","口径"],breadth_rows)
    breadth_comment=f'<p class="note">涨停{len(zt)}家、炸板{len(zb)}家、跌停{len(dt)}家；炸板率={len(zb)/(len(zt)+len(zb))*100:.1f}%（若分母非零）。全市场直接涨跌家数未取得，未用指数成分统计冒充全市场。</p>'
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
        movers.append([f"<strong>{html.escape(str(name))}</strong><br><span class='muted'>{c}</span>",fmt_pct(z.get("change_pct")),fmt_money_yi((z.get("amount_wan") or 0)*1e4),fmt_num(t.get("vol_ratio"),2)+"x",fmt_num(z.get("turnover_pct"),2)+"%",html.escape(str(reason)),"涨停/异动池" if c in pool_map else "固定池", "深入研究候选" if c in selected[:5] else "等待证据"])
    movers_html=html_table(["股票","涨跌","成交额","量比","换手率","板块/原因","信号","研究状态"],movers)
    news_top=ev[:10]
    macro=[e for e in ev if e.get("topic") in ("macro","policy","energy")][:6]
    macro_html="<ul>"+"".join(f"<li>{html.escape(e.get('headline',''))} <span class='muted'>[S4]</span></li>" for e in macro)+"</ul>" if macro else '<p class="muted">未取得明确宏观事件。</p>'
    macro_table=html_table(["变量","最新值","口径"],[["人民币/资金面","未取得","本次未补充可靠官方序列"],["全市场成交额",fmt_money_yi(sum((q(s).get("amount_wan") or 0)*1e4 for s,_ in [("sh000001",""),("sz399001","")])),"上证+深证指数成交额合计代理"],["涨停/炸板","%d / %d"%(len(zt),len(zb)),"打板池"]])
    cross_rows=[]
    try:
        us=json.loads((ROOT/"runs/us/2026-07-29/market_data/evidence.json").read_text())
        for s,n in [("SPY","标普500"),("QQQ","纳指100"),("SMH","半导体ETF")]:
            x=us.get(s,{})
            rows=x.get("rows",[]); close=rows[-1].get("close") if rows else None; prev=rows[-2].get("close") if len(rows)>1 else None
            cross_rows.append([n,fmt_num(close),fmt_pct(pct(close,prev)),"美股前一完整交易日 [S6]"])
    except Exception: cross_rows=[]
    cross_html=html_table(["资产","最新值","日变动","口径"],cross_rows) if cross_rows else '<p class="muted">未取得可靠跨市场数据。</p>'
    tech_rows=[]
    for s,n in INDEXES[:7]:
        z=tech.get(s,{})
        tech_rows.append([n,fmt_num(z.get("close")),fmt_num(z.get("ma20")),fmt_num(z.get("ma50")),fmt_num(z.get("ma200")),fmt_num(z.get("rsi"),1),z.get("macd") or "未取得",fmt_num(z.get("vol_ratio"),2)+"x","MA20上方" if z.get("close") and z.get("ma20") and z["close"]>z["ma20"] else "MA20下方"])
    technical_html=html_table(["标的","收盘","MA20","MA50","MA200","RSI14","MACD","量比","趋势"],tech_rows)
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
    sources='<ol><li>[S1] 腾讯财经行情与K线（HTTP）：指数、个股行情、日K及5分钟K、成交量、成交额和换手率；分钟数据按报告日缓存于 '+html.escape(str(data_dir/"minute_indices.json"))+'；as_of '+html.escape(now.strftime('%Y-%m-%d %H:%M Asia/Shanghai'))+'。</li><li>[S2] 东财 push2/ulist：指数成分涨跌统计；板块排名接口失败时不将空结果当作数据。</li><li>[S3] 东财 push2ex：涨停、炸板、跌停、昨日涨停池；交易日 '+report_date+'。</li><li>[S4] scan-market-news CN证据包：'+html.escape(str(news_dir))+'；'+html.escape(news_desc)+'。</li><li>[S5] 东财 datacenter：报告日龙虎榜；仅覆盖满足披露条件的证券。</li><li>[S6] global-stock-data已有证据包：美股前一完整交易日跨市场行情，仅在第12节作为外部联动参考。</li><li>[S7] 财联社电报：报告日交易时段带发布时间快讯，A股相关候选 '+str(intraday_event_count)+' 条；发布时间不自动等于事件发生时间，时间对齐不自动证明价格因果。</li><li>[S8] 巨潮资讯：重点异动股票公告及披露时间；仅正式公告标记为官方事件。</li></ol>'
    q0=q("sh000001"); q3=q("sh000300"); qg=q("sz399006"); sz=q("sz399001")
    turnover=(q0.get("amount_wan") or 0)+(sz.get("amount_wan") or 0)
    values={
      "REPORT_DATE_SHANGHAI":report_date,"MARKET_PHASE":"A股收盘复盘","GENERATED_AT_SHANGHAI":now.strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai"),"DATA_AS_OF":now.strftime("%Y-%m-%d %H:%M Asia/Shanghai"),"SESSION_SCOPE":"集合竞价、上午盘、下午盘；收盘后公告单独标注",
      "SSE_CLOSE":fmt_num(q0.get("price")),"SSE_CHANGE":fmt_pct(q0.get("change_pct")),"CSI300_CLOSE":fmt_num(q3.get("price")),"CSI300_CHANGE":fmt_pct(q3.get("change_pct")),"CHINEXT_CLOSE":fmt_num(qg.get("price")),"CHINEXT_CHANGE":fmt_pct(qg.get("change_pct")),"TOTAL_TURNOVER":fmt_money_yi(turnover*1e4),"TURNOVER_CHANGE":f'<span class="muted">成交额变化：未取得同口径前值</span>',
      "FIRST_READ_HTML":''.join(f'<div class="decision"><small>{a}</small><strong>{b}</strong></div>' for a,b in [("核心变化","指数与成长风格是否同步"),("主线",""+sec[0]["name"] if sec else "未取得"),("已反映/待验证","价格先行，公告与资金需验证"),("下一验证点","成交额、宽度与涨停梯队")]),
      "EXECUTIVE_SUMMARY_HTML":f'<p class="lead">{report_date} A股收盘：上证指数 {fmt_pct(q0.get("change_pct"))}，沪深300 {fmt_pct(q3.get("change_pct"))}，创业板指 {fmt_pct(qg.get("change_pct"))}；全市场成交额以沪深指数成交额合计为代理。涨停{len(zt)}家、炸板{len(zb)}家、跌停{len(dt)}家。[S1][S3]</p>',"MARKET_STATUS":"结构性轮动/风险偏好分化",
      "MARKET_OVERVIEW_TABLE_HTML":market_html,"RISK_APPETITE_HTML":f'<p class="note">以沪深300、中证1000、创业板指、涨跌停与炸板率联合判断风险偏好；全市场直接涨跌家数本次未取得。[S1][S3]</p>',
      "INTRADAY_SECTION_HTML":intraday_section,
      "MACRO_LIQUIDITY_TABLE_HTML":macro_table,"CROSS_ASSET_TABLE_HTML":cross_html,"MACRO_EVENTS_HTML":macro_html,
      "INDUSTRY_ROTATION_TABLE_HTML":sector_html,"THEME_ROTATION_HTML":themes,"STYLE_ROTATION_HTML":style_html,"SECTOR_TRANSMISSION_HTML":f'<p class="note">{flow_note}；如无明确板块资金字段，不使用“资金流入/流出”作为事实表述。</p>',
      "BREADTH_TABLE_HTML":breadth_html,"BREADTH_COMMENTARY_HTML":breadth_comment,"TECHNICAL_TABLE_HTML":technical_html,"TECHNICAL_COMMENTARY_HTML":'<p>核心指数日K至少取260个有效交易日，MA200不足时显示“未取得”；量比为报告日成交量除以前20个交易日平均成交量。[S1]</p>',
      "IDEA_FUNNEL_HTML":'<p class="note">异动候选采用固定观察池＋涨停/炸板/跌停池双通道；正文最多展示15只，原因缺失时不补写单一催化剂。</p>',"MOVERS_TABLE_HTML":movers_html,
      "DISCLOSURE_RECAP_TABLE_HTML":disclosure_html,"EVENT_CALENDAR_TABLE_HTML":future_html,"EARNINGS_ANALYSIS_HTML":'<p>本版未取得可统一核验的未来3个交易日预约披露明细；不将媒体标题写成正式业绩事实。</p>',
      "POSITIONING_SIGNALS_HTML":f'<p>报告日龙虎榜记录 {len(lhb)} 条；融资融券、股东户数和大宗交易本次未做全市场横截面补充，避免把单项数据当作全市场资金结论。[S5]</p>',"EVENT_ANALYSIS_HTML":'<p>重大事件只保留原始新闻候选，需以公司公告、交易所文件或监管材料二次核验。[S4]</p>',
      "UZI_CANDIDATES_HTML":'<p><strong>重点观察：</strong>'+"、".join(f"{q(c).get('name') or c}（{c}）" for c in selected[:5])+"。</p>","GLOBAL_LINKAGE_HTML":'<p>美股前一完整交易日的标普、纳指和半导体ETF数据作为外部联动参考；A股映射仍需下一交易日价格和成交额确认。[S6]</p>',
      "SCENARIO_TABLE_HTML":html_table(["情景","核心假设","触发条件","指数确认","宽度确认","失效条件","观察倾向"],[["偏强","成长风格止跌","创业板指收复MA20","沪深300不再创新低","涨停增加且炸板率下降","成交额继续萎缩","观察科技/高端制造"],["震荡/基准","板块轮动延续","指数在前收附近震荡","中证500相对稳定","涨跌停分化","权重与成长同步下破","等待主线确认"],["偏弱","风险偏好继续下降","沪深300与创业板同步走弱","成交额放大下跌","跌停增加、昨日涨停溢价转负","出现政策/公告反转","降低事件暴露"]]),"NEXT_SESSION_WATCHLIST_HTML":'<ul><li>成交额是否放大并得到上涨家数确认。</li><li>涨停梯队、炸板率和昨日涨停表现是否改善。</li><li>科技与高端制造代表股是否重新站上MA20。</li></ul>',
      "FINAL_CONCLUSION_HTML":f'<p class="lead">今日A股更接近<strong>板块轮动与风险偏好分化</strong>：指数方向、成交额代理、涨停梯队和代表股篮子需要联合观察。新闻提供潜在催化剂，但公告与资金口径仍是主要验证点。[S1][S3][S4]</p>',"SECTOR_SUMMARY_HTML":html_table(["类型","行业/主题","事实依据","资金方向口径","下一交易日验证"],[["相对强势",sec[0]["name"] if sec else "未取得","代表股篮子日/5日表现","价格/成交量代理","是否继续跑赢沪深300"],["相对弱势",sec[-1]["name"] if sec else "未取得","代表股篮子日/5日表现","价格/成交量代理","是否出现放量下破"],["重点关注","科技与高端制造","新闻候选+固定池异动","不是实际净流入","创业板指与成交额确认"]]),"MARKET_STAGE":"板块轮动","POSITIONING_BIAS_HTML":'<p>不追逐单日异动，等待成交额、宽度、公告和技术位置共同确认。</p>',"VALIDATION_SIGNALS_HTML":'<ol><li>如果上涨家数和成交额同步改善，那么结构性修复才有确认。</li><li>如果创业板指重新站上MA20，那么成长主线的持续性增强。</li><li>如果涨停增加且炸板率下降，那么短线赚钱效应改善。</li><li>如果昨日涨停池平均表现转负，那么题材持续性需要降级。</li><li>如果指数与成长风格同步跌破关键均线，那么今日轮动判断被推翻。</li></ol>',
      "EVIDENCE_LIMITATIONS_HTML":'<ul><li>东财板块排名/资金流接口本次出现连接风控时，行业表现使用代表股等权代理。</li><li>全市场直接涨跌家数、融资融券横截面和预约披露日历未完整取得。</li><li>盘中阶段已使用5分钟K重建；财联社和RSS时间戳仍主要是发布时间，未取得事件真实发生时间或官方文件时不称为已确认驱动。</li></ul>',"SOURCES_HTML":sources,
    }
    # Keep optional global linkage because the existing US evidence pack is available.
    out=REPORTS/f"A股收盘日报_{report_date}_Asia-Shanghai.html"
    render_template(TEMPLATE, values, out, strict=True)
    print(f"Wrote {out}")
    return out

def run(context):
    report_path = Path(main(
        context.report_date,
        template=context.template,
        reports_dir=context.reports_dir,
        runs_dir=context.runs_dir,
    ))
    match = re.search(r"(\d{4}-\d{2}-\d{2})", report_path.name)
    if not match:
        raise RuntimeError(f"无法从输出文件名识别报告日：{report_path.name}")
    return match.group(1), report_path


if __name__ == "__main__": main()
