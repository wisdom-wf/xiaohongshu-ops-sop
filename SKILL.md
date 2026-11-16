---
name: xiaohongshu-ops-sop
description: "小红书账号运营全流程 SOP：选题、标题与封面、图文/视频生产与质检、发布排期、14 维数据复盘、评论区服务、双池外联互动、付费生成预算门禁。Use when the user asks to 运营小红书、做选题/标题/封面、写笔记文案、图文卡片质检、发布/排期、分析账号数据/复盘、回复评论/私信、做同主题或爆款帖外联互动，或把小红书运营做成可审计的自动化闭环。Do not use for unrelated generic social-media advice."
metadata:
  version: "1.0.0"
---

# 小红书运营 SOP（Xiaohongshu Ops SOP）

把小红书运营当成一条**可审计的生产链**，而不是一组写稿提示词。
本技能沉淀自一个真实账号的长期运营实战：每个环节有硬规则、有检查脚本、有台账、有复盘口径。
所有数字参数（发布窗口、外联上限、评论字数等）都是**实战默认值**，集中在账号配置 `xiaohongshu-ops.yaml` 中，按账号数据调整，不写死在流程里。

Resolve `<skill-dir>` to the directory containing this `SKILL.md`.

## 快速开始

```bash
# 1. 初始化账号工作区（生成配置、数据目录、台账）
python3 <skill-dir>/scripts/init_workspace.py . --account-name "你的账号名"

# 2. 编辑 xiaohongshu-ops.yaml：定位、人群、内容支柱、地域词、搜索词、目标值
# 3. 校验配置与工作区
python3 <skill-dir>/scripts/doctor.py --config xiaohongshu-ops.yaml
```

依赖：Python 3.10+ 与 PyYAML（`python3 -m pip install pyyaml`）。其余脚本零第三方依赖。
浏览器执行层不绑定特定工具：任何能列出标签页、快照、点击、输入、上传的浏览器适配器（Playwright MCP、Chrome 扩展 MCP 等）都可以，见 `references/browser-execution.md`。

## 规则优先级

1. 用户当次明确指令。
2. `xiaohongshu-ops.yaml`（机器可读的目标与上限）。
3. 本 SKILL.md 与对应模式的 reference 文件。
4. 当日排期锁、发布队列、后台状态——只用来判断具体动作能否执行（槽位被占、已发布、风控等事实）。
5. 历史报告、旧 runbook、旧自动化记忆。只作证据，**不得覆盖当前规则**。

配置、文档、自动化与用户最新指令冲突时，以用户最新指令为准，并同步更新配置与文档，避免冲突复发。每次策略变化必须同时更新：配置 yaml、对应 reference、脚本默认参数、例行任务提示词。四处不一致时先停下审计。

## 铁律（Non-Negotiables）

- 不编造事实、数字、政策资格、个人经历、用户证言。没有来源的数字不写。
- 内容中不出现外链、联系方式、二维码、站外导流、推销话术。
- 不批量点赞、批量关注、群发私信、灌水评论，不绕过验证码与风控。
- 平台 UI 选择器视为不稳定：交互前重新快照，不复用旧选择器/旧坐标。
- **点击不等于成功**。必须回读页面结果、平台状态或可见记录。
- 缺失数据保持缺失（null），不估算、不按 0 求和凑报告。
- Cookie、Token、个人数据、私密评论、本地绝对路径不进公开仓库。
- 外联数字是**计划上限，不是配额**：没有合格候选、时间窗口错过、出现风控，宁可少做或不做。
- 一个账号同一时间只允许一个执行者做写操作。账号不符、有重复动作风险、发现另一个活跃写者时立即停止。
- 私信默认只读。只有当前任务点名会话并明确授权才发送。
- 自动化默认 manual-only：不自行创建、恢复、改写任何定时任务，须用户明确授权。
- 图文任一视觉负向规则（V01–V10）命中即硬失败，不得以「设计感」「留白高级」豁免。
- 每次浏览器运行结束必须**零残留任务标签页**：记基线、唯一前缀标注、成功/失败/中断都清理、只关本轮前缀的页面。
- 未获当前任务明确授权，不执行发布、评论、点赞、关注、私信、删除，也不执行任何付费生成。

## 模式（Modes）

### `audit` — 账号诊断 / 复盘

触发：「账号怎么样」「为什么没流量」「复盘」「数据分析」。

- 读 `references/analytics.md`。
- 按 `assets/templates/metrics-input.example.json` 的 schema 导出数据。
- 运行分析脚本，或人工执行同样的 14 维复盘。
- 结论格式：**现状 → 目标 → 差距 → 动作 → 算法环节 → 验收指标**。
- 不许停在仪表盘。必须落到下一次发布决策和两个具体动作。

```bash
python3 <skill-dir>/scripts/analyze_metrics.py \
  --config xiaohongshu-ops.yaml \
  --input data/metrics.json \
  --markdown data/reports/analysis.md \
  --json-out data/reports/analysis.json
```

口径红线：缺失值保持 null；每个比率带 `valid_n/total_n`；覆盖率 <90% 记「部分样本」不判绿；账号级区间值与笔记累计值不相加、不跨周期算达成率；每维输出 `status`（green/amber/red/none）与 `availability`（available/partial/descriptive/unavailable）。

### `plan` — 选题 / 排期

触发：「今天发什么」「排一周」「给我选题」。

- 读 `references/content-quality.md`。
- 从内容支柱、近期表现、选题池、评论区需求出发。
- 宁要一个具体的人类决策，不要一个大词。
- 每个选题写清：人群、场景、损失/收益、可执行动作、证据来源、最佳形式、为什么是现在。

### `create` — 内容生产

触发：写标题、脚本、图文卡片、封面文案、发布文案。

- 读 `references/content-quality.md`；图文必读 `references/image-visual-rules.md`。
- 标题、封面、前 3 秒/第 2 页、正文结构、立场句、转发对象、出处清单**一起产出**，不许单独设计标题。
- 视频：默认 45–60 秒、6–8 个信息点、缺口钩子、立场句、出处、关注理由（按配置）。
- 图文：默认 6–8 张卡、第 2 页兑现承诺、一卡一点、末卡可保存。
- 图文先写一句完整的首图承诺再拆版式；标题含「2笔/3项/5步」时必须有同等数量的独立兑现单元。
- 视频配音必须自然（真人或高质量自然 TTS），**禁止机械合成音进入任何交付物**，包括预览版/样片；配音留台账（来源、音色、段数、时长）。指定模板渲染时走模板真实渲染链，不得用静图+运镜冒充模板成片。
- 涉及付费生成（AI 图/视频/配音按次计费）先过预算门禁，见下文与 `references/budget-gate.md`。

### `review` — 审稿 / 风险检查

触发：「这条能不能发」「帮我审稿」「检查风险」。

- 读 `references/content-quality.md` 与 `references/source-policy.md`；图文加读 `references/image-visual-rules.md`。
- 先看 270px 首图预览和整套联系表，手机尺度读不清直接退回重做，不辩论美学。
- 先查标题/封面/开头一致性，再查事实、数字、合规、语气、反同质化。
- 不一致时改内容，不靠削弱标题掩盖。
- 无出处数字、编造经历、任一 V 规则失败，一律不得发布。

### `publish` — 发布 / 排期

触发：用户明确授权实发或挂定时。

- 读 `references/browser-execution.md`；视频/图文定时发布操作细节读 `references/publish-runbook.md`（含实测坑清单）。
- 执行配置的每日上限与排期锁。默认每天最多 1 图文 + 1 视频（上限不是 KPI，允许 0 条）；图文早间窗口、视频晚间窗口，分钟随机。
- 槽位被占或错过窗口就跳过，不发补偿性第三条，不窗口外补发。
- 挂定时前只读核对平台定时池是否已有同标题/同槽位；挂完回读 noteId 与定时时间，与台账一致才算完成。
- 发布前运行发布包检查；图文必须 `visual-review.json` 为 pass 且与当前标题/图片清单一致。
- 发布后从笔记管理页回读当日真实条数，防并行任务重复发布。
- 成功与失败路径都清理本轮标签页，核验残留为 0。
- 记录 noteId、标题、发布时间、形式、封面状态、清理结果、失败原因。

```bash
python3 <skill-dir>/scripts/check_publish_package.py publish/my-note \
  --config xiaohongshu-ops.yaml --kind image --json
```

### `engage` — 评论服务 / 外联

触发：回复评论、外联互动、维护外部互动。两项独立工作：

1. **自家评论区服务**：回复真实提问（见下节）。
2. **双池外联**：去别人的帖子下评论（见 `references/outreach.md`）。

外联开跑前必须先做详情页可达性探测（带 `xsec_token` 的链接能渲染正文才算可达；裸链接 300031 不构成封禁证据），详见 outreach.md §0。

## 双池外联（默认参数，全部可在配置调整）

| 池 | 每日计划上限 | 目的 |
|---|---:|---|
| 同主题帖 | 15 | 进入垂直对话，建立认知 |
| 爆款帖 | 15 | 进入高流量公共对话，赚主页回访 |
| 合计 | 30 | 计划上限；风控/候选不合格就提前停，不灌水凑数 |

执行纪律：

- 活跃窗口按配置（默认 `00:00-02:00` + `09:30-24:00`，晚间为流量高峰，60% 计划位优先落高峰段；`02:00-09:30` 静默不操作）。运营日按自然日归属，晚间场可跨零点，跨零点留言计入新一天配额。错过跳过，不补量。
- 两池穿插，不连做一池；单轮最多成功 2 条（推荐 1+1）；相邻 ≥10 分钟；滚动 60 分钟 ≤3 条。
- 同一作者 7 天不重复；同一句话/同一开头 7 天不复用。
- 先评论后提问，默认陈述句。问句每日 ≤5 条（同主题 ≤3、爆款 ≤2），每条最多一个问句且必须在最后一句，连续两条不得都带问句。
- 每条评论 ≤30 个汉字（只计汉字）、≤2 句；必须贴住原帖具体细节 + 自然同频感，不用客服腔/增长黑客腔。
- 禁止：「看我主页/关注我/互关/私信我」、通用「说得好学习了」、外链联系方式、蹭灾难疾病争议当事人。
- 禁止「打开即评论」：批量打开 3–5 篇 → 整体停留 30–60 秒（滚动、读 ≥3 条已有评论）→ 逐篇再停留 5–10 秒后留言。
- 被问「你是不是 AI」时大方承认，不装真人；回应话术在配置 `ai_admission.reply_text` 自定义，仅用于被追问场景。
- 可选功能（配置开关）：轻夸模式 `light_praise`、点赞感谢私信 `dm_thanks`（逐条人工批准）、关注回扫 `follow_back`（关注必须在下一轮留言之前，限制比留言更严）。

```bash
# 生成当日随机计划
python3 <skill-dir>/scripts/plan_outreach.py --config xiaohongshu-ops.yaml --format md
# 发送前质检评论批次
python3 <skill-dir>/scripts/lint_comments.py --config xiaohongshu-ops.yaml --input comments.json
# 成功后记账（--round-id 必填；状态不明记 skipped，不得记 success）
python3 <skill-dir>/scripts/outreach_ledger.py record \
  --config xiaohongshu-ops.yaml --round-id ROUND_ID --kind viral \
  --style shared_resonance --author-id USER_ID --author-name NICKNAME \
  --note-url NOTE_URL --text '评论原文' --result success
# 查看今日余量 / 作者冷却
python3 <skill-dir>/scripts/outreach_ledger.py status --config xiaohongshu-ops.yaml
python3 <skill-dir>/scripts/outreach_ledger.py check-author --config xiaohongshu-ops.yaml --author-id USER_ID
# 关注回扫队列（gate 只约束关注，不阻断留言）
python3 <skill-dir>/scripts/follow_queue.py gate --config xiaohongshu-ops.yaml
```

## 自家评论区服务

- 新笔记发布后 30 分钟内可补最多 1 条作者首评；每日首评数 ≤ 当日新笔记数；不批量回填历史 0 评论笔记。
- 首评必须补充一个具体判断或一个具体变量问题（资格、金额、材料、期限等），不是「欢迎评论」。
- 真实提问 2 小时内回复。结构：结论 → 一个依据/出处 → 最多一个反问；能自然收尾就不问。
- 不复读用户原话；不用「谢谢支持」当内容回复；只需致意的评论用表情或简短收尾。
- 回复只在通知页操作：点「回复」后核验输入框绑定目标评论（placeholder 含目标作者名），绑定不符跳过，不退化成顶层评论。

## 数据纪律

每日最小复盘覆盖 14 维：账号增长、流量规模、完播效率、封面效率、互动质量、关注转化、视频 vs 图文、主题表现、时长与钩子、流量来源与人群、标题公式、发布时段与标签、开头类型、互动与转发归因。

- 只比较同平台、同口径、同测量窗口。小样本（<3 条）标「样本不足」。
- 24h 看方向不判死刑；72h 只在曝光足够且点击率明显偏低时改一次封面/标题，一次只改一个变量。
- 自动报告只做描述与线索发现，不下因果结论。

## 反同质化

至少轮换：封面配色、封面版式、字体、视频视觉形态、片尾组合、文案结构（清单/问答/自测/对比）、发布分钟、系列框架。
连续 3 条不重复同一视觉组合；连续 2 条不重复文案结构；同日视频与图文不同题；同一内容 7 天内不换形式重发。垂类系列允许，批量生产感不允许。

## 付费生成预算门禁

凡按次/按秒计费的生成能力（AI 图片/视频/配音），一律先过闸门，详见 `references/budget-gate.md`：

1. **发布意图门禁**：未获用户明确「这条要发」，禁止任何付费生成；先做本地零成本可看版。
2. **预算契约**：报「次数 × 规格 × 时长 = 预估消耗」，拿到用户「同意」原话才开工；任一参数变化重新报、重新确认。
3. **档位默认最低**；升档必须写理由并经用户点头。
4. **换供应商 = 重新立项**，重走授权。
5. **零成本版先行**：视频先出本地静图+运镜完整可看版，用户认可节奏后才为必须运动的镜头付费；付费视频调用上限 `ceil(时长/10)+1`，硬上限 8 次。
6. **中断先查任务**，禁止直接重发；失败重试不超过 2 次；并发槽为 1 时单次只提 1 个任务。
7. **全量记账**：每次调用记台账，不可用产物必须写原因；每轮用平台积分差额校准单价。

```bash
python3 <skill-dir>/scripts/gen_guard.py plan --file plan.json        # 报预算（硬校验）
python3 <skill-dir>/scripts/gen_guard.py approve --file plan.json --confirm "<用户同意原话>"
python3 <skill-dir>/scripts/gen_guard.py record --vendor <vendor> --kind video --spec 768P --seconds 10 --result ok --usable
python3 <skill-dir>/scripts/gen_guard.py status
```

## 交付输出契约

每个实质性运营任务，返回五段，不许把决策埋在长解释里：

1. **Decision** — 该做什么 / 不做什么。
2. **Evidence** — 数据、来源或观察到的平台状态。
3. **Action** — 精确的下一步。
4. **Metric** — 怎么验收。
5. **Risk** — 什么会阻断或推翻执行。

涉及发布/图文时额外报告：首图完整承诺句、270px 预览与联系表复核结论、V01–V10 是否全过、标题数字与独立兑现单元、发布包检查结果、标签前缀与残留数；涉及付费生成时额外报告：授权编号、计划次数 vs 实际次数、浪费次数及原因。

## References

| 文件 | 内容 |
|---|---|
| `references/operating-model.md` | 日/周/月例行 SOP 与产出契约 |
| `references/content-quality.md` | 选题、标题、视频、图文、反同质化与发布前检查 |
| `references/image-visual-rules.md` | 图文视觉负向硬规则 V01–V10 与复核流程 |
| `references/outreach.md` | 双池外联执行规则、评论情绪模型、风控停止条件 |
| `references/analytics.md` | 14 维、公式、24h/72h 复盘 |
| `references/browser-execution.md` | 浏览器适配器原则、标签生命周期、发布/评论状态机 |
| `references/publish-runbook.md` | 网页端发布分步操作与实测坑清单 |
| `references/source-policy.md` | 事实、来源、隐私、许可与版本治理 |
| `references/budget-gate.md` | 付费生成预算门禁 R1–R8 |

## Scripts

| 脚本 | 用途 |
|---|---|
| `init_workspace.py` | 初始化账号工作区 |
| `doctor.py` | 校验配置、依赖与工作区健康 |
| `plan_outreach.py` | 生成随机化双池日计划（高峰优先、间隔/限流校验） |
| `lint_comments.py` | 评论批次质检：细节、同频、问句比例、长度、导流、重复 |
| `outreach_ledger.py` | 外联台账：冷却、窗口、间隔、轮次、问句上限硬校验 |
| `follow_queue.py` | 关注回扫队列：gate/pending/record/stats |
| `check_publish_package.py` | 发布包检查：标题、合规、数字、图文视觉复核硬门 |
| `analyze_metrics.py` | 14 维 Markdown/JSON 账号报告（覆盖率与可用性标注） |
| `gen_guard.py` | 付费生成预算闸门/台账/单价校准（零依赖） |

自测：`python3 -m unittest discover -s <skill-dir>/tests -v`