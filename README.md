# Stock Report

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB.svg)](pyproject.toml)

一个面向 A 股和美股的确定性收盘日报流水线：使用代码完成公开数据采集、指标计算、质量校验、证据留存和 HTML 渲染，而不是让一段大型 Prompt 控制全部流程。

当前状态：

- A 股：已接入；
- 美股：已接入；
- 港股：配置、模块和模板已预留，采集器尚未实现；
- LLM：默认关闭，只保留可选叙事增强边界。

## 特性

- 多市场统一 CLI、配置和运行目录；
- 报告日自动识别，也支持指定历史日期；
- MA20/50/200、RSI14、量比和区间收益等确定性指标；
- 美股常规收盘与 ET 16:00–20:00 盘后数据分离；
- 历史美股回放自动避免使用当前涨跌榜，防止未来数据穿越；
- A 股 5 分钟行情、盘中阶段重建和时间戳事件对齐；
- 证据文件、来源信息、质量结果和运行 manifest 分层保存；
- HTML 未渲染占位符、报告/证据日期、历史长度等自动检查；
- Prompt 只约束表达，不参与行情计算和质量判断；
- 原有脚本命令保留为兼容入口。

## 工作流

```mermaid
flowchart LR
    A["CLI：市场与日期"] --> B["Pipeline"]
    B --> C{"市场模块"}
    C -->|CN| D["A股采集与盘中重建"]
    C -->|US| E["美股采集与盘后补数"]
    D --> F["证据与确定性指标"]
    E --> F
    F --> G["质量校验"]
    G --> H["HTML 渲染"]
    H --> I["reports/ 最终日报"]
    G --> J["runs/ manifest"]
    K["可选 LLM 叙事"] -. 默认关闭 .-> H
```

## 环境要求

- Python 3.12 或更高版本；
- 推荐使用 [uv](https://docs.astral.sh/uv/)；
- 生成真实日报时需要访问公开市场数据端点；
- 项目核心流水线不依赖第三方 Python 运行库。

## 快速开始

### 使用 uv

```bash
git clone <your-repository-url>
cd stock
uv sync

uv run stock-report run --market cn
uv run stock-report run --market us
```

### 不安装项目，直接从源码运行

```bash
PYTHONPATH=src python -m stock_report.cli run --market cn
PYTHONPATH=src python -m stock_report.cli run --market us
```

也可以使用兼容入口：

```bash
python scripts/generate_a_close_report.py
python scripts/generate_us_close_report.py
```

## CLI 使用

生成最近完整交易日的日报：

```bash
stock-report run --market cn
stock-report run --market us
```

指定报告日：

```bash
stock-report run --market us --date 2026-07-31
```

只校验已有产物，不重新取数：

```bash
stock-report validate --market cn --date 2026-07-31
stock-report validate --market us --date 2026-07-31
```

查看完整帮助：

```bash
stock-report --help
```

## 项目结构

```text
stock/
├── config.yaml                  # 路径、市场、模板和流水线配置
├── pyproject.toml
├── src/stock_report/
│   ├── cli.py                   # CLI 入口
│   ├── pipeline.py              # 统一编排
│   ├── models.py                # 运行与质量模型
│   ├── common.py                # 配置、日期、时区和交易日历
│   ├── metrics.py               # 公共技术指标
│   ├── quality.py               # 质量门
│   ├── analysis.py              # 确定性分析与排序
│   ├── news.py                  # 读取结构化新闻包
│   ├── llm.py                   # 可选叙事接口，默认关闭
│   ├── render.py                # 严格 HTML 渲染
│   └── markets/
│       ├── cn.py                # A 股完整流程
│       ├── us.py                # 美股完整流程
│       └── hk.py                # 港股预留入口
├── prompts/                     # 可选叙事约束
├── templates/                   # 各市场 HTML 模板
├── scripts/                     # 旧命令兼容入口
├── tests/                       # 离线测试
├── runs/                        # 分市场、分报告日的运行证据
└── reports/                     # 最终 HTML 日报
```

更完整的迁移背景和设计复盘见 [`项目从Prompt到代码流水线_复盘总结.md`](项目从Prompt到代码流水线_复盘总结.md)。

## 配置

配置集中在 [`config.yaml`](config.yaml)。为了保持零运行依赖，当前文件使用 JSON 兼容的 YAML 子集。

```json
{
  "pipeline": {
    "llm_enabled": false,
    "fail_on_unresolved_placeholders": true,
    "write_manifest": true
  },
  "markets": {
    "cn": {"timezone": "Asia/Shanghai"},
    "us": {"timezone": "America/New_York"},
    "hk": {"timezone": "Asia/Hong_Kong"}
  }
}
```

如果未来启用 LLM 叙事，可参考 [`.env.example`](.env.example) 设置环境变量。当前 `llm_enabled=false`，日报生成不需要 API Key。

## 数据与运行产物

每次运行按照市场和报告日保存证据：

```text
runs/<market>/<report_date>/
├── manifest.json
├── market_data/
│   └── evidence.json
└── news/
```

A 股盘中流程还会生成：

- `minute_indices.json`：核心指数 5 分钟 K 线；
- `intraday_events.json`：带时间戳事件；
- `intraday_alignment.json`：事件与价格窗口对齐结果。

最终页面写入：

```text
reports/A股收盘日报_<date>_Asia-Shanghai.html
reports/美股收盘日报_<date>.html
```

`runs/` 与 `reports/` 是生成产物，默认不提交到 Git。

## 数据边界

当前市场模块使用的公开数据包括但不限于：

- A 股：腾讯行情与 K 线、东方财富行情与市场榜单、巨潮资讯公告、财联社时间戳快讯；
- 美股：Yahoo Chart、U.S. Treasury 收益率曲线、Nasdaq 财报日历；
- 新闻：读取预先生成的 `scan-market-news` 结构化事件包。

需要注意：

- `pipeline.py` 尚未自动执行 `scan-market-news`，新闻包需要预先生成；
- 港股采集器尚未实现；
- UZI 和 Public Equity Investing 尚未由主流水线自动调用；
- A 股官方宏观序列仍不完整；
- 免费公开端点可能限流、变更或暂时不可用；
- 历史报告的可复原程度取决于报告日缓存和数据源是否支持历史截面。

## 测试

测试默认离线运行，不会请求外部市场端点：

```bash
uv run python -m unittest discover -s tests -v
```

当前测试覆盖：

- 涨跌幅、MA、RSI 和量比；
- MA200 所需历史长度；
- 美股交易所休市日；
- HTML 未渲染占位符；
- 证据文件与报告日期；
- Pipeline 运行 manifest。

## Roadmap

- [ ] 将 `scan-market-news` 纳入主流水线；
- [ ] 增加 `--offline` 与 `--refresh` 模式；
- [ ] 统一保存原始响应、来源时间和内容哈希；
- [ ] 补充 A 股官方宏观序列；
- [ ] 实现港股行情、公告、南向资金和沽空流程；
- [ ] 扩展字段覆盖率与跨来源一致性质量门；
- [ ] 在确定性证据稳定后接入可选 LLM 叙事。

## 免责声明

本项目仅用于数据研究、软件开发和投资复盘，不构成投资建议、证券推荐、收益承诺或任何形式的受托管理服务。公开数据可能存在延迟、缺失、修订或接口错误；任何投资决策都应由使用者独立核验并自行承担风险。

## License

本项目采用 [MIT License](LICENSE)。
