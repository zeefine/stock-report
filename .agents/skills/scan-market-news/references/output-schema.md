# 输出数据合同

## `news_raw.jsonl`

每行一个JSON对象，至少包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `item_id` | string | 规范化URL和标题生成的稳定ID |
| `title` | string | RSS标题 |
| `url` | string | 清理追踪参数后的规范化URL |
| `published_at` | string/null | UTC ISO 8601 |
| `source_id` | string | 配置中的稳定来源ID |
| `source_name` | string | 来源名称 |
| `source_tier` | integer | 1最高，3最低 |
| `source_kind` | string | official/company/research/media/aggregator |
| `markets` | array | US/HK/CN/GLOBAL |
| `topic` | string | 行业或事件主题 |
| `summary` | string | RSS短摘要，不是全文 |
| `filter_labels` | array | 命中过滤词时记录 |
| `fetched_at` | string | UTC抓取时间 |

## `news_events.json`

顶层字段：

```json
{
  "schema_version": "1.0",
  "market": "US",
  "window": {
    "start": "2026-07-29T20:00:00Z",
    "end": "2026-07-31T00:30:00Z"
  },
  "events": []
}
```

事件至少包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `event_id` | string | 事件稳定ID |
| `headline` | string | 代表性标题 |
| `topic` | string | 主要主题 |
| `status` | string | candidate/confirmed/rejected |
| `first_published_at` | string/null | 最早时间 |
| `last_updated_at` | string/null | 最晚时间 |
| `source_ids` | array | 独立来源ID |
| `item_ids` | array | 聚类成员 |
| `corroboration_count` | integer | 独立来源数量 |
| `source_quality_score` | integer | 1–5 |
| `attention_score` | integer | 0–10，新闻优先级而非市场影响 |
| `market_impact_score` | null/integer | RSS阶段必须为null |
| `market_confirmation` | string | RSS阶段固定为`pending` |
| `filter_labels` | array | 聚合后的过滤标签 |

调用方验证行情后，可以在下游证据包增加：

```json
{
  "market_impact_score": 7,
  "market_confirmation": "confirmed",
  "price_evidence": {
    "assets": ["QQQ", "SOXX"],
    "window": "2026-07-30T14:00:00-04:00/2026-07-30T16:00:00-04:00",
    "observation": "事件后相关资产同步走弱且成交量放大"
  }
}
```

不要修改原始 `news_events.json`；把验证结果写入日报的统一 `evidence.json`。

## `news_sources.json`

每个来源记录：

- `source_id`
- `name`
- `url`
- `status`: success/empty/error/skipped
- `items_received`
- `error`
- `fetched_at`

## `manifest.json`

至少记录：

- Schema版本；
- 市场和UTC时间窗口；
- 配置文件路径和SHA-256；
- 运行时间；
- 来源总数、成功数、失败数；
- 原始条数、去重后条数、事件数；
- 过滤模式；
- 输出文件名。

## `news_dashboard.html`

由 `scripts/render_html.py` 读取上述四个数据文件生成。页面内嵌数据，不依赖CDN或HTTP服务器，包含：

- 市场、时间窗口和运行统计；
- 新闻主题分布；
- 候选事件列表；
- 可搜索、按主题和来源筛选的新闻明细；
- 全部来源的成功、空结果和失败状态。
