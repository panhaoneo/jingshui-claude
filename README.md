# jingshui-claude

把两份材料蒸馏成一套可执行的投资框架，并用同花顺 A 股财务与行情数据落地实现。

**两个信息源**

1. [静水2008 问答记录](https://panhaoneo.github.io/posts/jingshui2008/)（知乎作者「静水2008」的问答合集）——提供原则与纪律。
2. 威廉·欧奈尔《笑傲股市》原书第 4 版（机械工业出版社，宋三江译）——提供把原则变成数字门槛的方法。

**数据来源**：[HiThink-Tech/Financial-API](https://github.com/HiThink-Tech/Financial-API)，同花顺官方 A 股金融数据服务。

> 本项目是研究工具，不构成投资建议，不承诺任何收益。详见 `docs/03-融合投资框架.md` 末尾的风险声明。

---

## 文档

| 文件 | 内容 |
|---|---|
| [docs/01-jingshui2008-问答提炼.md](docs/01-jingshui2008-问答提炼.md) | 问答记录的结构化提炼，含明确剥离的部分和理由 |
| [docs/02-笑傲股市-CANSLIM提炼.md](docs/02-笑傲股市-CANSLIM提炼.md) | CAN SLIM 七要素的量化门槛、买卖规则、第 20 章 23 条法则 |
| [docs/03-融合投资框架.md](docs/03-融合投资框架.md) | **核心交付**：融合框架、两处冲突的处理、四层漏斗、已知边界 |
| [docs/04-数据接口映射.md](docs/04-数据接口映射.md) | 每条规则对应哪个端点、接口拿不到什么、调用纪律与参数坑 |

## 框架一览

```
L0  大盘闸门 M        派发日计数 + 均线排列  ->  决定能不能开仓、开多大
L1  行业景气筛选      板块 RS + 创新高       ->  锁定 3~5 个当下主线
L2  个股六因子        C A N S L I           ->  主线内的 α 候选池
L3  买点与仓位        回调后确认起涨、分批、止损
```

两个源头有两处实质冲突，框架里都做了明确处理而不是回避：

- **买回调 vs 买突破**：回调本身不是买入信号，回调之后的重新起涨才是。三个条件必须同时成立。
- **死拿 vs 20% 止盈**：不设固定止盈线，改为写死一张卖出触发表。

完整推理见 `docs/03-融合投资框架.md` 第 0 节。

## 安装与使用

无第三方依赖，Python 3.11+ 标准库即可。

```bash
export HITHINK_FINANCE_API_KEY=<你的 key>     # 申请: https://fuyao.aicubes.cn/admin/

python -m jingshui explain          # 打印规则速查表, 不需要 API Key
python -m jingshui doctor           # 逐个探测 11 个端点的可用性
python -m jingshui market           # L0 大盘闸门
python -m jingshui sectors          # L1 行业景气排名
python -m jingshui scan             # L0->L3 全流程, 结果写入 output/
```

常用参数：

```bash
python -m jingshui scan --tag cn_concept --top 3 --per-sector 30 --out output/
```

`scan` 的输出：

| 文件 | 内容 |
|---|---|
| `output/summary.json` | 大盘状态、主线板块、可下单/观察清单 |
| `output/sectors.json` | 全部板块景气评分明细 |
| `output/scores.json` | 每只候选股的六因子得分与可读理由 |
| `output/signals.json` | 买点判定结果与止损价 |

## 代码结构

```
jingshui/
  client.py        REST 客户端: 信封校验、错误码分类重试、Key 只走 Header
  indicators.py    纯计算: 均线、RS 百分位、派发日、量比、回撤、破位
  fundamentals.py  财务因子: 累计报表单季化、同比、加速度、ROE、时点纪律
  market.py        L0 大盘闸门
  sectors.py       L1 板块景气评分
  screener.py      L2 六因子评分
  rules.py         L3 买点、分批、止损、卖出触发、组合约束
  pipeline.py      编排层, 唯一发网络请求的地方, 大结果落盘
  cli.py           命令行
```

除 `client.py` 和 `pipeline.py` 外全是纯函数，因此测试完全离线，不需要 API Key。

## 测试

```bash
python -m unittest discover -s tests -t .
```

124 个用例，覆盖三类容易出错的地方：

- **A 股财报口径**：累计口径单季化、同比必须对去年同季、负基数不可比、时点纪律（用披露日而非报告期末判断可见性）。
- **接口契约**：`code != 0` 必须报错、`data` 为 `null` 不能当空结果、可修复错误不重试、限流退避重试、Key 不进 URL。
- **规则边界**：绝不向下加仓、止损基准是每一批而非均价、3 周涨 20% 触发 8 周锁定、止损优先于锁定期、组合上限与禁止杠杆。

## 先跑 doctor

`doctor` 逐个探测框架依赖的 11 个端点，用来区分三种失败：

| 输出 | 含义 |
|---|---|
| `[FAIL] API Key: 未找到` | Key 没配 |
| 全部 `网络不可达` | 域名被网络策略或防火墙拦截，换一台能直连 `fuyao.aicubes.cn` 的机器 |
| 个别 `code=2003` | 该能力未授权 |
| 个别 `code=1xxx` | 参数问题 |

## 已知限制

1. **机构认同度（I 因子）是代理变量**。接口不提供机构持股家数，用龙虎榜机构净额和成交额替代，权重已降到最低的 10 分。
2. **成分股只有当前快照**，没有历史调入调出序列，回测会有幸存者偏差。
3. **RS 样本总体是近似的**。默认只用主线板块成分股排名，比全市场排名更苛刻；可用 `ScanConfig.rs_reference_index` 指定宽基指数扩大样本。
4. **未做历史回测**。所有阈值直接来自两个信息源的原文，不是拟合出来的。
5. **涨跌停与 T+1** 会让 7%~8% 止损在跌停日无法成交，极端行情下实际亏损会超过止损线。
6. 分析师一致预期、管理层持股、回购公告等原书要求的数据，接口不提供，未实现。

完整边界见 `docs/03-融合投资框架.md` 第 2 节。
