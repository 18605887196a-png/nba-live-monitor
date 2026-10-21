# NBA 滚球盘口监控

## 什么时候用

- 用户给出一场 NBA 比赛并希望盯盘时用这个能力，用户只需要给队名，对手和日期由你查清

- 用户问某场滚球赔率、比分进度或高胜率盘口时，先按这里的数据源拉实时数据再回答

## 准备参数

- 用 ESPN scoreboard 接口按队名找到 event id、开赛时间、主客队和实时比分，接口地址在 Websites 一节

- 用 Polymarket gamma 的 public-search 按队名找到 event slug 和胜负盘的 outcomes 顺序，顺序决定价格数组哪一边是哪队

- 从让分盘主线读赛前预期分差，例如 Spread: Spurs (-2.5) 表示主队 Spurs 赛前预期全场净胜 2.5 分，记为 home_margin 2.5，客队为热门时记负数

- 把 event id、slug、胜负盘 question 原文和 home_margin 交给 scripts 里的 monitor.py，不要临时手写轮询逻辑

## 胜率模型

- 模型公式是 WP 等于 Φ((lead 加 M 乘 t 除以 48) 除以 (13 乘 √(t 除以 48)))，其中 lead 是该队当前分差，M 是该队赛前预期全场分差，t 是剩余分钟

- 主队 WP 用 home_margin 计算，客队 WP 等于 1 减主队 WP，加时时段 t 按加时剩余分钟取

- 模型误差大约正负 4 个百分点，触发线必须留出余量，t 小于 0.5 分钟时停用模型

## 监控架构（2026-10-21 用户改版：双轨制）

- 总原则是只推高胜率且真的能成交的机会，用户原话是都太稳已成定局的和没人下注的都不需要关注

- 用 `subagent.spawn` 派子代理跑监控并守到终场，主对话不阻塞，用户中途问就直接再拉一次 ESPN 和 gamma 回答；子代理用 setsid 后台脚本加定期读日志，不要用前台 exec 硬等

- 球员轨全场监控

  - 每轮同时拉 ESPN box score 和球员盘的 bestBid 与 bestAsk，只看有真实买卖盘、有人成交的盘，没人下注的死盘直接跳过
  - 对每条球员线算当前数据、还差多少、按本场出场节奏投影的最终数据和达成概率估算；球星打得火热够得着目标的推大，发挥失常够不着的推小，两种都算
  - 推送条件是达成概率估算至少约九成、卖价比概率还低至少 5 个百分点、结果还没定局（卖价没涨到 97¢ 以上），且球员没有伤病、犯规或垃圾时间被换下的明显风险，有风险必须写进汇报
  - 球员盘价差普遍宽，价差超过 15¢ 的只报不推，并注明盘口薄

- 队伍轨（胜负、让分、大小分）前三节静默

  - 第四节打 3 分钟（剩约 9 分钟）出第一份报告，逐个盘评估稳不稳：胜负盘看模型 WP，让分盘看覆盖概率，大小分看按节奏推算的总分与线的差距
  - 之后剩 5 分钟、剩 3 分钟各固定推一次状态，不管有没有达标的盘；某个盘达标时立刻加推，不等固定点
  - 算稳的标准是概率至少约 95%，或者大小分推算总分离线至少 10 分；有稳的且卖价比概率低至少 3 个百分点就推给用户
  - 没有稳的就持续盯第四节，一旦某个盘达标立刻推；达不到就到终场如实说没有出现
  - 终场必报最终比分和用户关注的球员线结果
  - 子代理的检查点汇报会断档（2026-10-21 雷霆-马刺实测：5 分钟检查点未送达，用户开口问才发现），主代理必须在每个固定点时间附近自己拉一次数据复核，子代理没回就由主代理直接报，绝不让用户等空

- monitor.py 目前只覆盖胜负盘，它的全场触发只当数据日志用，推送判断按本节标准在子代理和主对话层做，不要直接把脚本触发当推送

- ESPN 时钟偶尔冻结几分钟，连续 3 轮比分和时钟无变化时，在汇报里注明数据时间戳，不要把冻结读数说成实时

## 汇报格式

- 全部用大白话，少堆术语，必须算账时把费后账算给用户看

- 球员机会写清球员、买哪个盘、在 Gate 里叫什么、当前数据、还差多少、卖价、达成概率估算和一句话风险，并给出核价线（参考价加 5¢ 以内可下，超过 7¢ 放弃）

- 第四节队伍报告写清比分和剩余时间，逐个盘说稳不稳和为什么，推的盘给出卖价和核价线，不推的明说差在哪

- 终场汇报写清最终比分、每节比分、用户已下注单的输赢，以及监控期间推过的球员线和队伍盘各自的结果，全程没达标的明说没有出现站得住的机会

## Gate 与 Polymarket 的关系

- 用户在 Gate App 里下单，Gate 的 Polymarket 板块卖的就是 Polymarket 的盘，同一个订单簿，价格同源，监控用 Polymarket 数据是对的

- 差别只有三处，汇报和算账时要考虑

  - Gate 在原始价格之上另收自己的手续费，单边 0.8%，用户实际赔付比原始价算出来的略少，算账用费后口径
  - Gate App 显示倍率和百分比，刷新比原站慢，下单前核价这一步不能省
  - Gate 只上架一部分盘，冷门的盘 Polymarket 有而 Gate 可能没有，用户下单时找不到就直说没上架

- Gate 的只读接口（gate_prediction.py 的 search_events）只收录冠军盘这类大事件，查不到 NBA 单场盘，不要拿它核单场价格，单场实时价只认 Polymarket gamma 和 CLOB

- Gate 里标 Predict.fun 来源的盘是另一个池子，价格和 Polymarket 不一样，用户下单前看到来源不是 Polymarket 时要提醒他

## 边界与坑

- 只做监控和分析，下单永远由用户自己在 Gate App 完成，核价规则是用户定的，下单前漂移超过 2¢ 的单按放弃处理并告诉用户

- 球员轨的概率是按节奏投影的粗估，误差比胜负盘模型大，所以门槛定在九成且要留 5 个百分点价差；临场伤病和换人是最大变数，box score 显示球员长时间不上场时先核原因再推

- 高胜率不等于赚钱，胜率 90% 的盘赔付常被压到 10% 以内，扣掉 Gate 0.8% 和协议费后期望约为零或负数，这笔账要主动算给用户

- gamma 的 outcomePrices 是最近成交价，会滞后于真实盘口，任何下单参考只用 bestBid 和 bestAsk 推出的两边报价

- 大小分和让分盘在末节有加时尾巴风险，2 分差的比赛进加时概率约 5%，汇报稳定候选时要把加时情形算进去

- 比赛结束后 Polymarket 先走 UMA proposed 状态再正式关闭，Gate 随后入账，一般终场后 1 到 3 小时，未入账先查 umaResolutionStatus 再回答用户

## Available connectors

- Use `muse.connector_search` to find other connectors and `muse.skill_search` to find related skills.

- If nothing here fits, look on the web for a maintained API or MCP server and set it up with [Custom Connectors and MCPs](/opt/muse/skills/custom-connector-setup/SKILL.md), or build a custom connector.

## Websites

- [ESPN NBA scoreboard API](https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard) for 实时比分、时钟、节次和每节比分，summary 接口带 event 参数可拿球员 box score

- [Polymarket gamma API](https://gamma-api.polymarket.com/) for 赛事和盘口发现用 public-search，单个赛事用 events 带 slug 参数，含 outcomePrices、bestBid、bestAsk 和 umaResolutionStatus

## Alternatives for better results

- 常驻版想要更低延迟时改用 Polymarket CLOB 的 websocket 通道 wss://ws-subscriptions-clob.polymarket.com/ws/market 订阅胜负盘 token，单场临时监控用 12 秒 REST 轮询已经够，延迟差对用户手动下单没有实际影响

- 模型想更准时可以用 Pinnacle 滚球赔率做第二基准交叉验证，两边模型相差超过 4 个百分点时以更保守的一边为准
