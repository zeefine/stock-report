#!/usr/bin/env python3
"""Generate a compact, evidence-led US close report from public endpoints."""
from __future__ import annotations

import csv, html, io, json, math, re, statistics, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, date, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from stock_report.metrics import technical_snapshot as _technical_snapshot
except ModuleNotFoundError:  # defensive fallback for standalone module execution
    _technical_snapshot = None
from stock_report.news import load_news_events
from stock_report.render import html_table as _shared_html_table, render_template
from stock_report.common import next_us_trading_days

ROOT = Path(__file__).resolve().parents[3]
REPORT_DATE = ""
GENERATED_AT = ""
ASOF = ""
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
NY_TZ = ZoneInfo("America/New_York")
SH_TZ = ZoneInfo("Asia/Shanghai")
CALENDAR_DATES: list[str] = []
DATA_DIR = ROOT / "runs" / "us" / "market_data"
NEWS_DIR = ROOT / "runs" / "us" / "news"
TEMPLATE = ROOT / "美股收盘日报_template.html"
OUT = ROOT / "reports" / "美股收盘日报.html"

def get_json(url: str, params: dict | None = None, timeout: int = 25):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def get_text(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/csv,text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def chart(symbol: str, range_: str = "2y", interval: str = "1d", prepost: bool = False):
    try:
        j = get_json(f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol, safe='')}" ,
                     {"range": range_, "interval": interval, "includePrePost": str(prepost).lower()})
        result = (j.get("chart", {}).get("result") or [None])[0]
        if not result: return {"symbol": symbol, "meta": {}, "rows": []}
        q = (result.get("indicators", {}).get("quote") or [{}])[0]
        rows=[]
        for i, ts in enumerate(result.get("timestamp") or []):
            c = (q.get("close") or [None])[i]
            if c is None: continue
            dt = datetime.fromtimestamp(ts, timezone.utc)
            rows.append({"ts": ts, "date": dt.strftime("%Y-%m-%d"), "open": (q.get("open") or [None])[i],
                         "high": (q.get("high") or [None])[i], "low": (q.get("low") or [None])[i],
                         "close": c, "volume": (q.get("volume") or [0])[i] or 0})
        return {"symbol": symbol, "meta": result.get("meta") or {}, "rows": rows}
    except Exception as e:
        return {"symbol": symbol, "meta": {}, "rows": [], "error": str(e)}

def pct(a,b):
    return (a/b-1)*100 if a is not None and b not in (None,0) else None

def fmt(x, digits=2, suffix=""):
    if x is None: return "未取得"
    return f"{x:.{digits}f}{suffix}"

def sign_html(x, digits=2, suffix="%"):
    if x is None: return '<span class="muted">未取得</span>'
    cls="up" if x>0 else "down" if x<0 else "muted"
    return f'<span class="{cls}">{x:+.{digits}f}{suffix}</span>'

def latest_row(d, target=None):
    target = target or REPORT_DATE
    rows=d.get("rows",[])
    rr=[r for r in rows if r["date"]<=target]
    return rr[-1] if rr else (rows[-1] if rows else None)

def sma(vals,n): return sum(vals[-n:])/n if len(vals)>=n else None
def rsi(vals,n=14):
    if len(vals)<n+1:return None
    gains=[]; losses=[]
    for a,b in zip(vals[-n-1:-1],vals[-n:]):
        d=b-a; gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/n; al=sum(losses)/n
    return 100 if al==0 else 100-100/(1+ag/al)

def technical(d):
    rows=[r for r in d.get("rows",[]) if r["close"] is not None and (not REPORT_DATE or r["date"]<=REPORT_DATE)]
    if _technical_snapshot is not None:
        z=_technical_snapshot(rows)
        return {"close":z["close"],"day":z["day"],"r5":z["r5"],
                "sma20":z["ma20"],"sma50":z["ma50"],"sma200":z["ma200"],
                "rsi":z["rsi14"],"vol_ratio":z["volume_ratio20"],"last_date":z["last_date"]}
    closes=[r["close"] for r in rows]
    last=rows[-1] if rows else None
    return {"close":last["close"] if last else None, "day":pct(closes[-1],closes[-2]) if len(closes)>1 else None,
            "r5":pct(closes[-1],closes[-6]) if len(closes)>5 else None,
            "sma20":sma(closes,20),"sma50":sma(closes,50),"sma200":sma(closes,200),"rsi":rsi(closes),
            "vol_ratio":(rows[-1]["volume"]/statistics.mean([r["volume"] for r in rows[-21:-1]]) if len(rows)>21 and statistics.mean([r["volume"] for r in rows[-21:-1]]) else None),
            "last_date":last["date"] if last else None}

SYMS = ["SPY","QQQ","DIA","IWM","RSP","^VIX","XLK","XLC","XLY","XLP","XLE","XLF","XLV","XLI","XLB","XLRE","XLU","SOXX","SMH","IGV","XBI","ARKK","HYG","LQD","IEF","TLT","GLD","USO","BTC-USD","DX-Y.NYB",
        "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AMD","AVGO","NFLX","JPM","GS","XOM","CVX","UNH","LLY","NKE","BA","PLTR","CRWD"]
STOCK_POOL = ["AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AMD","AVGO","NFLX","JPM","GS","XOM","CVX","UNH","LLY","NKE","BA","PLTR","CRWD"]

def earnings_day(d):
    try:
        j=get_json("https://api.nasdaq.com/api/calendar/earnings",{"date":d})
        rows=((j.get("data") or {}).get("rows")) or []
        return [{"symbol":r.get("symbol"),"name":r.get("name"),"time":r.get("time"),"eps":r.get("epsForecast"),"cap":r.get("marketCap")} for r in rows]
    except Exception:return []

def treasury(report_date: str):
    year = report_date[:4]
    url=("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
         f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv")
    try:
        rows=list(csv.DictReader(io.StringIO(get_text(url))))
        target = date.fromisoformat(report_date)
        eligible=[]
        for candidate in rows:
            try:
                observed=datetime.strptime(candidate.get("Date", ""), "%m/%d/%Y").date()
            except ValueError:
                continue
            if observed <= target:
                eligible.append((observed, candidate))
        row=max(eligible, key=lambda item:item[0])[1] if eligible else {}
        return {"row":row,"y2":float(row["2 Yr"]),"y10":float(row["10 Yr"]),"spread":float(row["10 Yr"])-float(row["2 Yr"]),"url":url}
    except Exception as e:return {"row":{},"error":str(e),"url":url}

def screener(scr):
    try:
        j=get_json("https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved",{"scrIds":scr,"count":20})
        res=(j.get("finance",{}).get("result") or [{}])[0]
        return res.get("quotes") or []
    except Exception:return []

def news_pack():
    return load_news_events(NEWS_DIR)

def _next_weekdays(start: str, count: int) -> list[str]:
    return next_us_trading_days(start, count)

def _resolve_report_date(requested: str | None) -> str:
    if requested:
        date.fromisoformat(requested)
        return requested
    now=datetime.now(NY_TZ)
    rows=chart("SPY","1mo","1d",False).get("rows",[])
    dates=[row["date"] for row in rows if row.get("date")]
    if dates and dates[-1]==now.date().isoformat() and now.time()<time(16,15):
        dates=dates[:-1]
    return dates[-1] if dates else now.date().isoformat()

def _after_hours_close(data: dict) -> float | None:
    values=[]
    for row in data.get("rows",[]):
        observed=datetime.fromtimestamp(row["ts"],timezone.utc).astimezone(NY_TZ)
        if observed.date().isoformat()==REPORT_DATE and time(16,0)<observed.time()<=time(20,0):
            values.append(row)
    return values[-1]["close"] if values else None

def main(
    report_date: str | None = None,
    *,
    template: Path | None = None,
    reports_dir: Path | None = None,
    runs_dir: Path | None = None,
):
    """Run the US report with runtime-owned dates and output paths."""
    global REPORT_DATE, GENERATED_AT, ASOF, DATA_DIR, NEWS_DIR, TEMPLATE, OUT, CALENDAR_DATES
    REPORT_DATE=_resolve_report_date(report_date)
    run_root=Path(runs_dir) if runs_dir else ROOT/"runs"
    report_root=Path(reports_dir) if reports_dir else ROOT/"reports"
    TEMPLATE=Path(template) if template else ROOT/"美股收盘日报_template.html"
    DATA_DIR=run_root/"us"/REPORT_DATE/"market_data"
    NEWS_DIR=run_root/"us"/REPORT_DATE/"news"
    OUT=report_root/f"美股收盘日报_{REPORT_DATE}.html"
    CALENDAR_DATES=_next_weekdays(REPORT_DATE,3)
    generated=datetime.now(SH_TZ)
    GENERATED_AT=generated.strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai")
    ASOF=f"{REPORT_DATE} 20:00 ET（常规收盘截至 {REPORT_DATE} 16:00 ET；盘后截至 {REPORT_DATE} 20:00 ET）"
    DATA_DIR.mkdir(parents=True,exist_ok=True); report_root.mkdir(parents=True,exist_ok=True)
    data={}
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs={ex.submit(chart,s,"2y","1d",False):s for s in SYMS}
        for f in as_completed(futs): data[futs[f]]=f.result()
    # after-hours and latest quote proxy for movers/candidates
    after={}
    for s in ["SPY","QQQ","AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AMD","AVGO","NFLX","PLTR","CRWD"]:
        d=chart(s,"5d","5m",True); rows=d.get("rows",[])
        # Yahoo chart timestamps are UTC; after 20:00 ET = 00:00 UTC next day in July.
        after[s]=_after_hours_close(d)
    for d in [REPORT_DATE,*CALENDAR_DATES]:
        data[f"earnings_{d}"]=earnings_day(d)
    data["treasury"]=treasury(REPORT_DATE)
    latest_available=(data.get("SPY",{}).get("rows") or [{}])[-1].get("date")
    if latest_available == REPORT_DATE:
        data["gainers"]=screener("day_gainers"); data["losers"]=screener("day_losers")
        data["mover_scope"]="yahoo_current_market_screener"
    else:
        snapshots=[(symbol,technical(data.get(symbol,{}))) for symbol in STOCK_POOL]
        snapshots=[item for item in snapshots if item[1].get("day") is not None]
        snapshots.sort(key=lambda item:item[1]["day"], reverse=True)
        data["gainers"]=[{"symbol":symbol,"shortName":symbol} for symbol,_ in snapshots[:8]]
        data["losers"]=[{"symbol":symbol,"shortName":symbol} for symbol,_ in snapshots[-8:][::-1]]
        data["mover_scope"]="fixed_pool_historical_snapshot"
    # Secondary pull for every mover shown in the正文 table: keep regular and
    # after-hours prices separate, and leave unavailable values explicitly blank.
    mover_symbols = dict.fromkeys(x.get("symbol") for x in
                                 data.get("gainers",[])[:8] + data.get("losers",[])[:8]
                                 if x.get("symbol"))
    for s in mover_symbols:
        if not data.get(s, {}).get("rows"):
            data[s] = chart(s, "2y", "1d", False)
        if s not in after:
            d=chart(s,"5d","5m",True); rows=d.get("rows",[])
            after[s]=_after_hours_close(d)
    data["after"] = after
    data["report_date"] = REPORT_DATE
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    (DATA_DIR/"evidence.json").write_text(json.dumps(data,ensure_ascii=False,indent=2))
    return render(data, news_pack())

def q(symbol): return technical(DATA.get(symbol,{}))

def table(headers, rows):
    return _shared_html_table(headers, rows)

def render(data, events):
    global DATA
    DATA=data
    spy=q("SPY"); qqq=q("QQQ"); vix=q("^VIX")
    t=data.get("treasury",{}); spread=t.get("spread")
    sectors=[("XLK","科技"),("XLC","通信"),("XLY","可选消费"),("XLP","必选消费"),("XLE","能源"),("XLF","金融"),("XLV","医疗"),("XLI","工业"),("XLB","材料"),("XLRE","房地产"),("XLU","公用事业")]
    secrows=[]
    for s,n in sectors:
        z=q(s); secrows.append((s,n,z.get("close"),z.get("day"),z.get("r5")))
    secrows.sort(key=lambda x:(x[3] if x[3] is not None else -999), reverse=True)
    sector_html=table(["ETF","板块","收盘","日变动","5日变动"],[[f"<strong>{s}</strong>",n,fmt(c),sign_html(d),sign_html(r)] for s,n,c,d,r in secrows])
    ov=[("SPY","标普500",q("SPY")),("QQQ","纳指100",q("QQQ")),("DIA","道指",q("DIA")),("IWM","罗素2000",q("IWM")),("RSP","等权标普",q("RSP"))]
    market_html=table(["代码","指数/代理","收盘","日变动","5日变动","MA20","MA200"],[[s,n,fmt(z["close"]),sign_html(z["day"]),sign_html(z["r5"]),fmt(z["sma20"]),fmt(z["sma200"])] for s,n,z in ov])
    treasury_html=table(["期限","收益率"],[["2Y",fmt(t.get("y2"),3,"%")],["10Y",fmt(t.get("y10"),3,"%")],["10Y−2Y",sign_html(spread,3,"个百分点")]])
    cross=[("HYG","高收益债",q("HYG")),("LQD","投资级债",q("LQD")),("IEF","7–10Y国债",q("IEF")),("TLT","长期国债",q("TLT")),("GLD","黄金",q("GLD")),("USO","原油",q("USO")),("BTC-USD","比特币",q("BTC-USD")),("DX-Y.NYB","美元指数代理",q("DX-Y.NYB"))]
    cross_html=table(["代理","名称","收盘","日变动","5日变动"],[[s,n,fmt(z["close"]),sign_html(z["day"]),sign_html(z["r5"])] for s,n,z in cross])
    # movers
    movers=[]
    for label,key in [("涨幅", "gainers"),("跌幅","losers")]:
        for x in data.get(key,[])[:8]:
            s=x.get("symbol") or ""; z=technical(data.get(s) or {})
            movers.append((label,s,x.get("shortName") or x.get("longName") or "",z.get("close"),z.get("day"),z.get("vol_ratio"),data.get("after",{}).get(s)))
    movers_html=table(["方向","代码","名称","收盘","日变动","量比","盘后"],[[lab,s,html.escape(n)[:48],fmt(c),sign_html(ch),fmt(v,2,"x"),fmt(ah)] for lab,s,n,c,ch,v,ah in movers])
    # fixed pool technical table
    pool=STOCK_POOL
    techrows=[]
    for s in pool:
        z=q(s); techrows.append([f"<strong>{s}</strong>",fmt(z["close"]),sign_html(z["day"]),fmt(z["sma20"]),fmt(z["sma50"]),fmt(z["sma200"]),fmt(z["rsi"],1),fmt(z["vol_ratio"],2,"x")])
    tech_html=table(["代码","收盘","日变动","MA20","MA50","MA200","RSI14","量比"],techrows)
    # earnings
    rec=data.get("earnings_"+REPORT_DATE,[])
    rec_major=[r for r in rec if r.get("symbol") in {"MSFT","META","GOOGL","TSLA","V","CMCSA","F","QCOM"}]
    if not rec_major: rec_major=rec[:8]
    rec_html=table(["代码","公司","时段","EPS预期"],[[r.get("symbol"),html.escape(r.get("name") or ""),r.get("time") or "未取得",r.get("eps") or "未取得"] for r in rec_major]) if rec_major else '<p class="muted">未取得。</p>'
    cal=[]
    for d in CALENDAR_DATES:
        for r in data.get("earnings_"+d,[])[:12]: cal.append([d,r.get("symbol"),html.escape(r.get("name") or ""),r.get("time") or "未取得",r.get("eps") or "未取得"])
    cal_html=table(["日期","代码","公司","时段","EPS预期"],cal) if cal else '<p class="muted">未取得。</p>'
    # news
    top=events[:12]
    macro=[e for e in events if e.get("topic")=="macro"][:5]
    topic_counts={}
    for e in events: topic_counts[e.get("topic") or "other"]=topic_counts.get(e.get("topic") or "other",0)+1
    news_html="<ol>"+"".join(f"<li>{html.escape(e.get('headline',''))} <span class='tag'>{e.get('topic','')}</span> <span class='muted'>[S4]</span></li>" for e in top[:8])+"</ol>"
    macro_html="<ul>"+"".join(f"<li>{html.escape(e.get('headline',''))} <span class='muted'>[S4]</span></li>" for e in macro)+"</ul>" if macro else '<p class="muted">未取得明确宏观事件。</p>'
    topic_str="；".join(f"{k} {v}条" for k,v in sorted(topic_counts.items(), key=lambda x:-x[1])[:6])
    # scenarios/candidates
    candidates=[s for s in ["NVDA","MSFT","META","AAPL","AMZN","TSLA","AMD","AVGO","PLTR","CRWD"] if q(s)["close"] is not None]
    scenario_html=table(["情景","触发条件","观察资产","失效条件"],[["风险偏好延续","SPY守住MA20且QQQ相对强势","QQQ、XLK、SMH","QQQ跌破MA20"],["轮动扩散","RSP相对SPY转强、IWM不再落后","RSP、IWM、XLI","小盘重新走弱"],["利率冲击","10Y−2Y上行且TLT下跌","TLT、HYG/LQD","收益率回落"],["事件驱动","财报/监管 headline 改变预期","财报股、行业ETF","消息被市场快速否定"]])
    first=[("市场方向", "标普与纳指收盘及5日趋势"), ("主线", secrows[0][1] if secrows else "未取得"), ("风险", "利率、波动率与信用代理"), ("明日", "财报与盘前新闻验证")]
    first_html="".join(f'<div class="decision"><small>{a}</small><strong>{b}</strong></div>' for a,b in first)
    status="偏风险偏好" if (spy.get("day") or 0)>0 and (qqq.get("day") or 0)>0 else "分化/防御"
    values={
      "REPORT_DATE_ET":REPORT_DATE+" ET","MARKET_PHASE":"常规收盘复盘 + 盘后补充","GENERATED_AT_SHANGHAI":GENERATED_AT,"DATA_AS_OF":ASOF,"SESSION_SCOPE":"Regular close 与 after-hours 分开",
      "SPY_CLOSE":fmt(spy["close"]),"SPY_CHANGE":sign_html(spy["day"]),"QQQ_CLOSE":fmt(qqq["close"]),"QQQ_CHANGE":sign_html(qqq["day"]),"US10Y_VALUE":fmt(t.get("y10"),3,"%"),"US10Y_CLASS":"up" if (t.get("y10") or 0)>0 else "muted","US10Y_CHANGE":"10Y−2Y "+fmt(spread,3,"个百分点"),"VIX_VALUE":fmt(vix["close"]),"VIX_CHANGE":sign_html(vix["day"]),
      "FIRST_READ_HTML":first_html,"EXECUTIVE_SUMMARY_HTML":f'<p class="lead">{REPORT_DATE} 常规交易日，主要指数表现为：SPY {sign_html(spy["day"])}、QQQ {sign_html(qqq["day"])}；板块日线最强为 {secrows[0][1] if secrows else "未取得"}，新闻包共 {len(events)} 个候选事件（主题分布：{html.escape(topic_str)}）。</p>',"MARKET_STATUS":status,
      "MARKET_OVERVIEW_TABLE_HTML":market_html,"RISK_APPETITE_HTML":f'<p class="note">RSP/SPY 与 HYG/LQD 用作风险偏好代理；10Y−2Y={fmt(spread,3,"个百分点")}，正值代表曲线正斜率。[S1][S2]</p>',
      "INTRADAY_TIMELINE_HTML":'<p class="muted">本版不对5分钟线重建逐时因果；以收盘、盘后和新闻时间戳做事件对照。</p>',"DRIVER_TRANSMISSION_HTML":f'<p>新闻候选事件集中在：{html.escape(topic_str)}。将其视为待验证信息，不直接推断价格因果。[S4]</p>',
      "TREASURY_TABLE_HTML":treasury_html,"CROSS_ASSET_TABLE_HTML":cross_html,"MACRO_EVENTS_HTML":macro_html,
      "SECTOR_ROTATION_TABLE_HTML":sector_html,"THEME_ROTATION_HTML":f'<p>板块主题按ETF日/5日收益排序；当前首要观察：{secrows[0][1] if secrows else "未取得"}，相对弱势：{secrows[-1][1] if secrows else "未取得"}。[S1]</p>',"SECTOR_TRANSMISSION_HTML":'<p class="note">板块资金流入/流出未使用未经验证的资金流字段；以ETF价格与成交量代理表述。</p>',
      "BREADTH_PROXY_TABLE_HTML":table(["代理","结果"],[["上涨板块ETF",f"{sum(1 for x in secrows if (x[3] or 0)>0)}/{len(secrows)}"],["QQQ相对SPY",sign_html((qqq["r5"] or 0)-(spy["r5"] or 0))+"（5日变化差）"],["RSP相对SPY",sign_html((q("RSP")["r5"] or 0)-(spy["r5"] or 0))],["HYG相对LQD",sign_html((q("HYG")["r5"] or 0)-(q("LQD")["r5"] or 0))]]),
      "TECHNICAL_TABLE_HTML":tech_html,"TECHNICAL_COMMENTARY_HTML":f'<p>核心ETF已获取2年日K，MA200可计算；固定观察池使用相同口径。SPY收盘位于MA20 {"上方" if spy["close"] and spy["sma20"] and spy["close"]>spy["sma20"] else "下方"}。[S1]</p>',
      "IDEA_FUNNEL_HTML":('<p class="note">异动采用固定观察池＋Yahoo Day Gainers/Losers 双通道；量比为报告日成交量÷前20交易日平均成交量，盘后字段与常规收盘分开。[S1]</p>' if data.get("mover_scope")=="yahoo_current_market_screener" else '<p class="note">历史回放不使用当前涨跌榜；异动按固定观察池在报告日的涨跌幅排序。量比为报告日成交量÷前20交易日平均成交量，盘后字段与常规收盘分开。[S1]</p>'),"MOVERS_TABLE_HTML":movers_html,
      "EARNINGS_RECAP_TABLE_HTML":rec_html,"EARNINGS_CALENDAR_TABLE_HTML":cal_html,"EARNINGS_ANALYSIS_HTML":'<p>财报日历仅记录公开日历与EPS预期；未取得正式财报全文时不补写业绩结论。[S3]</p>',
      "POSITIONING_SIGNALS_HTML":'<p class="muted">本版未取得逐股票完整CBOE希腊字母或FINRA空头横截面，故不填充具体期权/做空数值。</p>',"EVENT_ANALYSIS_HTML":'<p>候选公司事件见新闻事件包，需二次核验原始公告后再纳入交易判断。[S4]</p>',
      "UZI_CANDIDATES_HTML":'<p><strong>重点观察：</strong> '+"、".join(candidates)+"。</p>","CHINA_HK_LINKAGE_HTML":'<p class="muted">本报告未发现需要调用 A 股/港股数据层的明确联动证据，保留新闻层观察，不扩展跨市场行情。</p>',
      "SCENARIO_TABLE_HTML":scenario_html,"NEXT_SESSION_WATCHLIST_HTML":'<ul><li>盘前核验财报与重大新闻时间戳。</li><li>观察QQQ/SMH相对SPY是否延续。</li><li>观察10Y−2Y与TLT/HYG-LQD是否确认风险偏好。</li></ul>',
      "FINAL_CONCLUSION_HTML":f'<p class="lead">结论：市场状态为<strong>{status}</strong>。指数方向、利率曲线和板块相对强弱需要联合观察；新闻事件仅作为候选催化剂，不能替代价格与公告证据。[S1][S2][S4]</p>',
      "SECTOR_SUMMARY_HTML":table(["排序","板块","日变动","5日变动","关注"],[[i+1,n,sign_html(d),sign_html(r),"重点观察" if i<3 else "跟踪"] for i,(s,n,c,d,r) in enumerate(secrows)]),
      "MARKET_STAGE":"趋势与轮动并存，等待财报/宏观验证","POSITIONING_BIAS_HTML":'<p>以观察和分层验证为主；不替代个人持仓与风险预算。</p>',"VALIDATION_SIGNALS_HTML":'<ol><li>SPY/QQQ是否守住MA20。</li><li>RSP与IWM相对强弱是否改善。</li><li>10Y−2Y是否继续上行。</li><li>HYG/LQD是否确认信用风险偏好。</li><li>重大财报与新闻是否出现可核验原始文件。</li></ol>',
      "EVIDENCE_LIMITATIONS_HTML":'<ul><li>新闻源为公开RSS聚合，部分源抓取失败或仅有标题。</li><li>盘后价格只在 Yahoo chart 能确认时间戳时填充，其余显示“未取得”。</li><li>市场宽度为代理，不等同全市场涨跌家数。</li></ul>',
      "SOURCES_HTML":f'<ol><li>[S1] Yahoo Finance Chart API：行情、日K、技术指标、ETF与异动代理；<code>query2.finance.yahoo.com/v8/finance/chart</code>，as_of {REPORT_DATE} 20:00 ET。</li><li>[S2] U.S. Treasury Daily Treasury Par Yield Curve：截至 {REPORT_DATE} 的最近可得观测；<code>home.treasury.gov/.../daily-treasury-rates.csv</code>。</li><li>[S3] Nasdaq Earnings Calendar API：{REPORT_DATE} 至 {CALENDAR_DATES[-1] if CALENDAR_DATES else REPORT_DATE}；<code>api.nasdaq.com/api/calendar/earnings</code>。</li><li>[S4] scan-market-news 本地证据包：<code>runs/us/{REPORT_DATE}/news/</code>；窗口与来源成功率以该目录 manifest.json 为准。</li></ol>'
    }
    render_template(TEMPLATE, values, OUT, strict=True)
    print(f"Wrote {OUT}")
    return OUT

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


if __name__=="__main__": main()
