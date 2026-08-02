---
name: scan-market-news
description: 聚合、规范化、去重、聚类和初步排序美股、港股、A股及跨市场公开新闻，输出可供市场日报使用的结构化事件包、原始新闻JSONL、来源账本和运行清单。用于“扫描今日市场热点”“聚合最近交易日新闻”“为收盘日报准备新闻证据”“寻找宏观、行业、公司与监管事件”等任务；不用于单独获取行情、把新闻直接解释为价格因果、抓取付费正文或替代个股估值研究。
---

# Scan Market News

将公开RSS新闻转换成可追溯的候选事件。把搜索、时间过滤、规范化、去重和初步排序交给脚本；把语义判断、行情验证和投资传导分析留给后续研究流程。

## 执行流程

1. 确定市场、新闻窗口和输出目录。
   - 支持 `US`、`HK`、`CN`、`ALL`。
   - 日报必须传入明确的UTC起止时间；不要用“最近几天”代替交易日窗口。
   - 同时记录市场日期、生成时间和所用时区。
2. 读取 `references/sources.json`，只抓取目标市场可用且启用的RSS源。
   - 默认配置使用用户提供的106个高信号源。
   - 需要使用其他源池时，传入 `--config /path/to/sources.json`；脚本兼容 `hint`、`redline_keywords` 和 `fetch.timeout` 格式。
3. 运行：

```bash
python3 scripts/scan_market_news.py \
  --market US \
  --start 2026-07-29T20:00:00Z \
  --end 2026-07-31T00:30:00Z \
  --output-dir runs/us/2026-07-30/news
```

使用外部自定义源池：

```bash
python3 scripts/scan_market_news.py \
  --market ALL \
  --start 2026-07-29T07:00:00Z \
  --end 2026-07-30T22:00:00Z \
  --config /absolute/path/to/sources.json \
  --output-dir runs/all/2026-07-30/news
```

4. 运行输出校验：

```bash
python3 scripts/validate_output.py runs/us/2026-07-30/news
```

5. 使用四个数据文件生成自包含HTML：

```bash
python3 scripts/render_html.py runs/us/2026-07-30/news
```

   默认生成 `runs/us/2026-07-30/news/news_dashboard.html`。页面不依赖外部CDN或本地服务器，可直接用浏览器打开。
6. 读取 `news_events.json`，按事件优先级选择候选热点。
7. 使用行情、成交量、板块相对强弱和官方文件验证候选事件。
   - 没有价格证据时保留 `market_impact_score: null`。
   - 不得把相关性直接写成因果。
   - 不得因为转载数量多就认定影响重大。
8. 将验证后的事件并入日报统一证据包和来源账本，再交给研究分析工作流。

## 输出

脚本在目标目录生成：

- `news_raw.jsonl`：规范化后的逐条新闻；
- `news_events.json`：跨源去重、聚类后的候选事件；
- `news_sources.json`：来源状态、成功/失败、条数和错误；
- `manifest.json`：参数、时间窗口、统计和输出版本。
- `news_dashboard.html`：由上述四个数据文件生成的自包含可视化页面。

需要字段定义时读取 `references/output-schema.md`。

## 证据边界

- RSS只提供标题、链接、时间和短摘要，不抓取或保存付费正文。
- 官方源、公司披露和交易所来源优先；媒体源用于发现与补充背景。
- 单一媒体报道可以进入候选池，但重大结论应由官方材料或至少两个独立可靠来源确认。
- 匿名消息、社交媒体和聚合转载不得作为已确认事实。
- 标题缺少时间时允许保留，但标记 `published_at: null`，不得参与严格交易日归因。
- RSS失败、超时、XML错误和空结果必须写入来源账本，不得静默忽略。
- 过滤词默认只添加标签，不静默删除；只有用户明确要求时才使用 `--filter-mode drop`。

需要来源等级、版权和验证规则时读取 `references/source-policy.md`；需要评分解释时读取 `references/scoring-policy.md`。

## 失败处理

- 部分来源失败：继续执行，在 `news_sources.json` 披露失败源。
- 全部来源失败：返回非零退出码，不生成“无热点”结论。
- 新闻不足：输出较少事件，不降低来源标准或扩大时间窗口凑数。
- 日期不清：停止日报归因；要求调用方提供UTC起止时间。
