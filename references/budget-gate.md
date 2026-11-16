# 付费生成预算门禁

> 适用：任何按次/按秒/按积分计费的生成能力（AI 图片、AI 视频、付费 TTS 等）。
> 来源：一次真实事故——一条 42 秒视频耗掉整个供应商额度 + 约 2000 积分，
> 31 次付费提交、8 次未产出可用物。本门禁把「先授权、再预算、后记账」变成硬流程。

## 八条硬规则（违反即停手）

- **R1 发布意图门禁**：未获用户明确「这条要发」，禁止任何付费生成。
  可做：脚本、分镜、字幕、本地剪辑、免费模型查询。
- **R2 预算契约**：报「次数 × 规格 × 时长 = 预估消耗」，取得用户「同意」原话才开工。
  任一参数变化（分辨率/时长/段数/模型/供应商）→ 重新报、重新确认。
- **R3 档位默认最低**：视频默认最低可用档（如 768P），图片默认 1K。
  升档必须写 `escalation_reason` 并经用户点头。注意各供应商档位枚举不同，写错会直接 400。
- **R4 换供应商 = 重新立项**：切换供应商即重走 R1/R2，不得延续上一家的授权。
- **R5 零成本版先行**：任何视频必须先出「本地静图 + 运镜（Ken Burns）」完整可看版，
  用户认可节奏后，才允许为必须运动的镜头付费。
  付费视频调用上限 `ceil(目标时长/10)+1`，硬上限 8 次。
- **R6 中断先查任务**：中断/超时/报错时先查询任务状态、记任务号入台账，禁止直接重发；
  悬空任务当天核对是否扣费。
- **R7 失败不超两次重试**：参数类报错必须读懂可选值再改，禁止「猜着升档」。
- **R8 全量记账**：所有付费调用写台账，`usable=false` 的必须写原因。

补充纪律：

- 并发槽 = 1 时禁止批量提交；单次只提 1 个任务，拿到结果再提下一个。
- 一个回合只做一件事；预计长时间完不成就停手，回报现状。
- 用户自有 API（按字符计费的 TTS 等）可不走积分门禁，但**必须留台账**：
  音色、风格、段数、各段真实时长、总时长。

## 闸门脚本 gen_guard.py

零依赖（仅标准库）。台账默认写在当前工作目录 `data/` 下，
可用环境变量 `XHS_OPS_WORKSPACE` 指定账号工作区根目录。

```bash
# 1. 写计划并报预算（校验不过 exit 1，不得开工）
python3 <skill-dir>/scripts/gen_guard.py plan --file plan.json

# 2. 把预算表原样贴给用户，拿到「同意」原话后才核销（授权默认 4 小时有效）
python3 <skill-dir>/scripts/gen_guard.py approve --file plan.json --confirm "<用户原话>"

# 3. 每次真实调用后记账；超额会 exit 1 并要求重新报预算
python3 <skill-dir>/scripts/gen_guard.py record \
  --vendor <vendor> --kind <image|video|audio> --spec <规格> \
  --seconds <n> --result <ok|fail> --usable

# 4. 每轮结束用平台积分差额校准单价
python3 <skill-dir>/scripts/gen_guard.py calibrate --before <余额> --after <余额> --note "<本轮>"

# 5. 查当前授权与已用额度
python3 <skill-dir>/scripts/gen_guard.py status
```

## plan.json 示例

```json
{
  "task": "示例：读评论区竖屏视频",
  "target_duration_s": 42,
  "note_publish_confirmed": true,
  "draft_reviewed": true,
  "calls": [
    {"kind": "video", "vendor": "vendor-a", "resolution": "768P", "segments": [10, 10, 8]},
    {"kind": "image", "vendor": "vendor-a", "size": "1024x1024", "count": 3}
  ]
}
```

字段说明：

- `note_publish_confirmed`：用户已明确这条要发（R1）。
- `draft_reviewed`：零成本草稿已给用户看过节奏（R5）。
- `segments`：视频分段时长；调用次数按段数计。
- `escalation_reason`：升档时必填（R3）。

预算上限可在 `data/gen-budget.json` 覆盖（`max_image_calls`、`max_video_calls_hard`、
`video_seconds_per_call`、`approval_ttl_hours` 等）。

## 交付时必须报告

- 授权编号（plan hash）、计划次数 vs 实际次数。
- 浪费次数及原因（`usable=false` 的台账记录）。
- `gen_guard.py status` 输出。