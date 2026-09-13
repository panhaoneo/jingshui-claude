# 景气α · A股每日选股投研框架

把 **静水2008 的原则**（当下景气行业的核心 α、回调后分批买、对了死拿错了砍掉）与 **欧奈尔《笑傲股市》CAN SLIM 的数字门槛** 蒸馏成一套可被数据执行的规则，每天收盘后由 GitHub Actions 自动跑全市场，结果以 GitHub Pages 页面发布，供研究查看。

- 策略来源：[`docs/01`](docs/01-jingshui2008-问答提炼.md) 静水2008 问答提炼 · [`docs/02`](docs/02-笑傲股市-CANSLIM提炼.md) CAN SLIM 提炼
- 规则定义：[`docs/03-融合投资框架.md`](docs/03-融合投资框架.md)（本项目的实现规格）
- 数据接口：[`docs/04-数据接口映射.md`](docs/04-数据接口映射.md)
- 全部阈值：[`config.yaml`](config.yaml)

> 研究工具，不构成投资建议。框架未经历史回测，阈值来自原文而非拟合。

## 每日流程

```
L0 大盘闸门 M     沪深300 / 创业板指 / 上证指数：MA50/MA200 + 25 日派发日计数 → 进攻 / 观望 / 防守 + 仓位上限
L1 行业景气       同花顺 90 个二级行业：120 日 RS 40 + 距新高 30 + 60 日动能 20 + 趋势 10 → 前 5 主线
L2 个股六因子     主线成分股：硬性排除 → C/A/N/S/L/I 打分 → 总分 ≥70 且 C/A/N/L 均不为 0
L3 买点           趋势未破 + 回撤 8%~25% + 放量上攻/突破平台 → 可下单；否则进观察池
持仓体检          portfolio.yaml 中的持仓：止损线 + 卖出触发表
信号追踪          历史每日买点信号的后续表现（按 8% 止损结算）
公告链接          展示列表个股附「最新定期报告 / 招股书」巨潮 PDF 外链（仅便于阅读，不下载）
```

另有一份 **全市场领军（非主线）研究池**：不属于主线板块、但全市场 RS ≥90 且距新高 ≤10% 的股票，同样打分，只供研究，不产生买点（框架规定 L2 只在主线内选股）。

## 数据来源

| 用途 | 主源：同花顺金融数据服务（需 Key） | 备用：`--provider free`（零鉴权） |
|---|---|---|
| 全市场日线 | Market Dumps：10 年全量 / 近 10 日增量 Parquet（3 次请求拿全市场） | 腾讯 fqkline 逐只（约 5500 次请求） |
| 复权 | 复权事件 dump，本地推算前复权 | 腾讯前复权 |
| 当日补数 | dump 未发布时用全市场快照拼当日 K 线 | — |
| 指数 / 板块 | 指数日线、同花顺行业目录与成分股 | 腾讯指数、东财行业（串行限流） |
| 财报 | 利润表（季度累计、年度）、资产负债表 | 新浪财报 |
| I 因子代理 | 龙虎榜机构专用席位净额 | 无 |
| 流通市值 | 腾讯行情（尽力而为） | 腾讯行情 |
| 公告文档 | 巨潮资讯公开查询端点（与数据源无关，零鉴权）：最新定期报告 / 招股书 PDF 外链 | 同左 |

取数纪律遵循 docs/04：成功 = HTTP 200 且 `code == 0`；参数/认证类错误不重试；3002 数据未就绪不补零；4001/5xxx 指数退避最多 3 次；`null` 不当 0；Key 只走 Header。

## 部署到 GitHub（一次性）

1. 在 GitHub 新建仓库（公开或私有均可；私有仓库的 Pages 需要付费计划），把本目录推上去：

   ```bash
   git remote add origin https://github.com/<你的用户名>/<仓库名>.git
   git push -u origin main
   ```

2. **Settings → Secrets and variables → Actions → New repository secret**
   名称 `HITHINK_FINANCE_API_KEY`，值为同花顺金融数据服务的 API Key。

3. **Settings → Pages → Build and deployment → Source** 选 **GitHub Actions**。

4. **Settings → Actions → General → Workflow permissions** 选 **Read and write permissions**（Actions 需要把每日结果提交回仓库）。

5. **Actions → 每日选股 → Run workflow** 手动跑一次。首次运行会下载全市场 10 年日线（约 180MB）建本地库，之后每天只拉增量。
   页面地址：`https://<你的用户名>.github.io/<仓库名>/`

之后每个工作日北京时间 18:40 自动运行（21:30 再跑一次兜底：数据未就绪时重试，已有结果则跳过）。节假日自动跳过。

手动运行时可选：指定日期、强制重跑、按时间顺序补跑最近 N 个交易日（`backfill_days`）、切换数据源。

## 本地运行

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows；macOS/Linux 用 .venv/bin/pip
```

API Key 放在环境变量 `HITHINK_FINANCE_API_KEY`，或用户级文件 `~/.config/hithink-finance/credentials.env`（内容 `HITHINK_FINANCE_API_KEY=...`）。**不要写进仓库。**

```bash
python -m jingshui run                    # 最近一个已收盘交易日
python -m jingshui run --date 2026-09-11 --force
python -m jingshui backfill --days 60     # 补跑最近 60 个交易日（生成信号追踪历史）
python -m jingshui run --provider free    # 零鉴权备用源（慢，且无机构净额）
python -m pytest -q                       # 单元测试
python -m http.server 8765 --directory site   # 本地预览页面
```

退出码：`0` 成功/跳过，`3` 行情尚未更新到目标交易日，`1` 其他错误。

## 目录

```
jingshui/
  client.py         同花顺 REST 客户端（错误码分类、退避、预签名下载断点续传）
  providers/        hithink（主源）/ free（备用）数据源
  adjust.py         复权事件 → 前复权因子
  technicals.py     全市场技术指标（RS、距新高、量比、均线）
  market.py         L0 大盘闸门
  sectors.py        L1 行业景气
  fundamentals.py   单季化、同比、时点纪律、ROE、增速回落
  scoring.py        L2 硬性排除与六因子
  signals.py        L3 买点、持仓卖出触发
  cninfo.py         巨潮公告链接（最新定期报告 / 招股书 PDF，零鉴权）
  pipeline.py       每日执行清单、信号追踪、输出
site/               GitHub Pages 页面（静态，无构建）
  data/latest.json  最新一日完整结果
  data/daily/       每日归档
  data/charts.json  最新一日个股 K 线（不归档）
config.yaml         全部阈值
portfolio.yaml      持仓体检（可选）
```

## 实现中的取舍（与原文不一致或原文未定义之处）

| 事项 | 处理 |
|---|---|
| 六因子权重合计 | 原文权重 20+15+20+10+20+10 = **95**（M 作闸门不计分），但写"总分 100、入选线 70"。本实现保留原权重，满分 95、入选线 70 不变（等于略微收紧）。如需按 100 分折算，改 `stocks.pass_score` 为 66.5。 |
| 主线的"创新高必要条件" | 原文注明板块指数创新高是必要条件之一，但未给数值。本实现取距 250 日高点 ≤5%（`sectors.require_near_high`），不满足的板块不进主线；设为 `null` 可关闭。弱市里主线可能少于 5 个甚至为 0，这正是"没有主线就空仓"。 |
| 因子内部分配 | 原文只给了"起分线/满分线"。本实现：C = 净利 70%（25% 起半分、50% 满分）+ 营收 20% + 加速 10%；A = 3 年增速 60%（按达标年数）+ ROE 40%；S = 量比 80% + 市值 20%；L = RS 80% + 板块内前 20% 占 20%；I = 龙虎榜机构净买入 60% + 成交额分位 40%。 |
| 同比基数 ≤0 | 扭亏或亏损扩大时同比无意义，记为缺失（C 因子为 0），不补数。 |
| 阶段高点 | 取最近 120 个交易日（不含当日）的最高价；回撤深度 = 阶段高点到其后最低点。 |
| 起涨确认的有效期 | 当日或近 2 日内确认、且现价未超出买点 5%，都算有效。 |
| RS 样本总体 | 用 Market Dumps 覆盖全市场（原文第 5 条边界"只用主线成分股构成样本"在主源下不再存在；free 源仍是全市场但更慢）。 |
| 财报披露日 | 同花顺的 `report_date` 对被后续报告重述的旧期次会更新为重述日，补跑历史时会偏晚（保守，不会引入未来函数）；free 源按法定截止日推算。 |
| 公告文档链接 | 同花顺官方不提供公告原文，为展示列表个股附巨潮「最新定期报告 / 招股书」PDF 外链，仅作阅读入口，不下载 PDF 入库；链接取"当前最新"，补跑历史时不做时点还原。`config.yaml` 的 `docs.enabled` 可整体关闭。 |

## 已知边界

见 docs/03 §2：只在有主线的市场里有效；I 因子是代理变量；A 股财报频率低；涨跌停与 T+1 使止损可能无法成交；成分股只有当前口径（补跑有幸存者偏差）；未经历史回测。
