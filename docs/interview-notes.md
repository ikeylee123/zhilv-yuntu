# Interview story and technical deep dives

Evidence: [final benchmark](final-benchmark.md), [architecture](final-architecture.md), [claim safety](claim-safety.md). Use measured offline numbers with their scope; do not memorize all dataset sizes as a technology checklist.

## Two-minute story / 两分钟口述

我做的是一个约束感知的旅行规划后端。起点是一个很实际的问题：大模型能生成看起来合理的行程，但地点可能没有可靠身份，两个景点之间可能来不及，用户修改一天时还可能把其他天的安排一起改掉。所以我的重点是让生成结果可校验、让修改范围可控。

系统把几种职责分开：LLM 负责生成草稿，或者从服务端给出的候选中选一个 ID；工具负责提供 POI 和驾驶路线观察；确定性代码根据约束与证据输出 valid、invalid 或 unverifiable。比如只有二十分钟间隔，路线估计却要三十五分钟，就明确报冲突；拿不到路线证据则保留未知，不能当成通过。初始草稿可以带着不通过报告保存，但局部重规划只有通过完整校验后才提交。

最难的部分是编辑时保持状态一致。我给活动保留稳定 ID，服务端拥有权威行程和版本号，只允许单日单项修改。变更先经过授权范围检查，复用仍适用的事实、重新采集受影响的路线，再检查未修改内容并做完整校验，最后用 CAS 防止覆盖并发更新。模型选错 ID、工具超时或版本过期，都不能直接写入状态。

验证上，我建立了二十四个固定离线跨层场景，失败重规划的六个场景都保留了当前权威状态，实际检查没有发现未授权变更。RAG 也保留来源和版本证据，不过关键词基线在十二个语义偏好样例上的 Recall@6 仍然为零，我没有把它包装成检索优势。

现在我把项目定位为注重可靠性的应用 AI 系统。已经验证的是离线规则、协议和状态边界；真实模型质量、实时价格、未来路况和生产部署还需要独立验证。

## Deep-dive questions

### Q1. 为什么不直接让 LLM 自己调工具？

当前需求是受限生成和单项编辑，不需要开放的自主工具循环。服务端可以明确控制工具参数、候选授权、预算和超时，把模型的不确定输出约束在 draft 或 candidate ID。模型选择不等于执行权限。未来如果出现需要多步探索的明确任务，再比较自主调用的收益和可观测性成本。

### Q2. 为什么 Validator 不能调 API？

Validator 接收相同约束、计划、事实快照和 evaluation_time 时，应得到相同结论。如果在里面访问 API，重试、网络波动和时效就进入规则判断，无法准确回放失败。采集层处理工具错误和新鲜度，Validator 只处理证据是否足以支持约束；CAS 前还能复用同一套纯校验。

### Q3. 为什么需要 unverifiable，而不是 valid/invalid 两态？

“路线估计三十五分钟但间隔二十分钟”是可证实的冲突；“路线工具超时”是缺少证据。把后者视为 valid 会错误授权，把后者视为已证实 invalid 又混淆原因。三态让调用方知道需要修改安排，还是需要补证据。存在明确硬错误时优先 invalid；没有硬错误但存在 unknown 时是 unverifiable。

### Q4. 为什么需要 authoritative state + CAS？

如果信任浏览器回传的整份 itinerary，客户端可以覆盖服务端约束、事实或另一人的更新。权威状态在数据库里，请求只携带意图与 base_plan_version。完成工具/模型调用后，CAS 用预期版本做条件更新；并发期间已有新版本时拒绝旧写入。竞争场景的正确保护目标是获胜写入方的记录，而不是恢复旧版本。

### Q5. 为什么局部修改不重新生成整份行程？

全量生成容易改变用户没要求修改的内容，也难以判断哪些旧事实仍有效。稳定活动 ID 和确定性 patch 让授权范围可检查，未涉及的日期、活动和约束保持不变；只处理允许的费用/交通派生字段。代价是当前限制为单日、单项景点操作，不能声称完整路线优化。

### Q6. 为什么 MCP 不是主生产路径？

当前 Python 服务可以直接调用同一套 typed ToolProvider。绕到 MCP 再回来会增加协议生命周期和错误处理，没有解决现有瓶颈。MCP 的价值在外部消费者和一致的工具契约。二十八个离线协议场景通过真实本机 Streamable HTTP 验证与 direct provider 的可比语义，后端观察来自 replay。

### Q7. 为什么没有 LangGraph？

这里没有经过验证的多轮图执行需求。生成/选择是一次调用，授权、事实采集、合并、校验、提交主要是确定性编排。引入图框架需要说明它具体改善了什么恢复或协调问题；当前没有这样的证据。这是需求取舍，不是认为框架本身不好。

### Q8. 为什么没有 Multi-Agent？

多个模型角色并不会自动提供更可靠的地点身份或状态一致性，反而增加共享状态协调和失败组合。当前把不同职责落实为明确的服务边界和数据模型即可。如果未来确有可以独立求解、并且有可验证汇合规则的任务，再评估多 Agent。

### Q9. 为什么没加 BM25/RRF？

当前关键词基线在语义偏好上存在明确弱点，但仅看到弱点不能证明某个新组件有效。需要在固定标签、语料和预算下比较向量、词法和融合检索，再决定是否值得引入。Phase 4 的目标是建立证据，没有为了扩大技术栈而修改冻结检索算法。

### Q10. 如何证明 RAG 不是黑盒？

每个 chunk 带 source/path/heading/ID/content hash，索引和检索配置有版本，缓存命中也要验证当前语料成员身份。结果明确记录检索方法、降级事件和错误。评估先检查标签是否仍属于语料，再报告正例分母、Recall/MRR、无结果控制和跨城市污染。可追溯不等于内容实时正确，所以静态攻略不进入动态 PlanningFacts。

## Story A — Route-grounded feasibility

| Element | Answer |
|---|---|
| Problem | 行程时间看起来连贯，但模型的交通描述无法证明两个活动之间来得及。 |
| Constraints | 保持规则可回放；只支持已有 driving 工具；不把未知、过期或端点不一致的观察当成已知。 |
| Initial failure mode | 仅按字符串时间或模型给出的“30 分钟”判断，缺少可绑定的来源与地点身份。 |
| Architecture decision | 将 typed tool observations、server collector、PlanningFacts 和 pure validator 分开。 |
| Implementation | 明确 POI ID/坐标、米/秒单位、时间戳和 query identity；collector 绑定计划快照；validator 比较相邻 timed pairs 的间隔和驾驶估计。 |
| Adversarial test | 20-minute gap / 35-minute estimate；超时；walking unsupported；旧事实或坐标错配不能静默通过。 |
| Result | 48-case 冻结 route 回归无 mismatches；E2E 四个路线场景符合三态预期，工具失败不产生已知路线。 |
| Trade-off | 只能证明当前可发现 timed pairs 对已有估计的一致性；初始计划往往只有一个 timed attraction/day，不能声称整趟行程可行。 |
| Future work | 独立验证真实 provider 的时效与覆盖，再考虑更多交通方式、营业时间等事实；不把它们说成本轮已实现。 |

## Story B — Constraint-preserving replanning

| Element | Answer |
|---|---|
| Problem | 用户只想替换一个景点，全量模型重写可能改变预算约束、其他日期或另一请求刚写入的版本。 |
| Constraints | 客户端和模型都不能提交任意权威 JSON；工具调用可能超时；候选或版本可在选择过程中失效。 |
| Initial failure mode | “让 LLM 重写然后保存”缺少可检查的修改边界，也会扩大无意变更范围。 |
| Architecture decision | 稳定活动 ID + server-owned state + 授权候选 + deterministic patch + full validation + CAS。 |
| Implementation | prepare 绑定 trip/version/day/operation；选中后再做 grant/version/detail grounding；根据新旧路线查询复用/重采集证据；完整 preservation projection 校验受保护内容与派生费用。 |
| Adversarial test | 虚构 candidate ID、唯一 must_visit 删除、工具超时、候选文本要求改预算、模型执行期间另一 CAS 成功。 |
| Result | 50-case 冻结 replanning 回归与 36-case selector contract 无 mismatches；E2E 六个失败重规划保持当前行，成功修改只产生授权变化。 |
| Trade-off | 单日单项操作、single-process 候选缓存；竞争测试不是分布式部署保证，回放选择不是模型偏好质量测量。 |
| Future work | 先做真实使用场景和多进程授权存储实验，再决定是否扩展操作范围或编排方式。 |

## Numbers to use selectively

优先讲 24 个跨层固定场景中的 6/6 失败重规划保护，再按话题选 48 个路线回归或 60 个 RAG 样例。1308 是完整后端的参数展开实例数，420 是收集到的独立测试函数/方法数，不能都叫“真实场景”。不同评估集不合并成总体 AI accuracy。所有数字的环境、日期与哈希见 benchmark report。
