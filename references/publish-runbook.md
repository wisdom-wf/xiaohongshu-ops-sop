# 发布 Runbook（视频/图文定时发布标准作业）

> **为什么有这份文件**：网页端定时发布有一串隐蔽的坑（详见 §5），全部来自实战踩坑记录。
> 流程骨架在 `browser-execution.md` §3，但操作细节如果散落在日志里，
> 每个 agent 每次发布都要重新摸 DOM。本文件是发布环节的操作依据，每次发布按它走，不重新探索。
>
> ⚠️ 平台前端会改版：文中的选择器/坐标是实测快照，使用前重新验证，失效时以实时快照为准并更新本文。
>
> **登记规则**：发布中遇到本文没有的问题 → 解决后**当场**把坑写进 §5，并注明日期。
> 下一个 agent 发布前必须通读 §5 一次（约 2 分钟）。

## 0. 发布前置检查（硬门）

1. `python3 <skill-dir>/scripts/doctor.py --config xiaohongshu-ops.yaml` 通过，浏览器适配器健康
2. 发布包齐全：成片 mp4（时长按配置）、封面 png、正文 txt、标题；`check_publish_package.py` 通过
3. 发布窗口按配置（默认图文早间、视频晚间）；**定时发布不受窗口限制**，但定时时间必须与用户确认过
4. 每日额度按配置（默认 1 图文 + 1 视频），先从笔记管理页核对当天已发数
5. 新开**专用标签**做发布（带本轮唯一前缀），绝不占用其他任务或用户正在用的标签

## 1. 视频定时发布标准流程

```text
tabs list 建基线 → 新开 XHS-PUB 标签打开发布页
→ upload 视频（§2.1）→ 等转码完成（约 20–25s，115MB 以内）
→ 设主封面（§2.2）→ 填标题（§2.3）→ 填正文（§2.4）
→ 开定时开关 + 写时间（§2.5）→ 终检（§3）→ 点发布（§2.6）→ 回读验证（§4）
```

## 2. 分步操作要点

### 2.1 上传视频
- `upload` 工具按**位置参数**传：`path=<绝对路径> tabId=<id>`（没有 `files=` 这个参数）。
  工具自动找 accept 匹配的 file input；视频页只有一个 accept=video 的 input，直接传即可。
- 上传后**必须截图确认**「上传成功/高清」且没有「上传失败 网络错误」红字。
- 遇到「上传失败 网络错误」：点「重新上传」按钮 → 再次 `upload path=... tabId=...` → 等 20s 复查。
  这已发生过一次（2026-10-29），重传即恢复，不算风控。

### 2.2 设置主封面（最繁琐的一步，按此路径走，勿自创）
1. 主封面槽在左侧预览区；**hover 才出现「编辑封面」按钮**——先真实 click 槽位触发 hover，再 eval 取
   `.cover-edit-entry` 实时中心坐标，click 该坐标。
2. 封面编辑器打开后（`.cover-modal` 存在）：**先真实 click「上传」按钮**（`.upload-btn` 中心），
   **紧接着立刻** `upload path=<封面png> tabId=<id>`——两次动作之间不要 sleep。
   eval 注入 DataTransfer + synthetic change **无效**（Vue 不理合成事件），别走这条路。
3. 画布出现封面后 eval 确认，点「完成」按钮（实时取坐标），回到发布页再截图确认主封面槽已更新。
4. **PK 封面**：开关开着时槽位会被自动推荐图占满（3/3），且删除按钮被画布层挡住点不到、
   自定义图塞不进。**不要恋战**：直接关 PK 开关（`.cover-plugin-title .d-switch` 真实坐标点击），
   改走主封面单图路线。PK 三图与自定义图二选一。

### 2.3 填标题
- `input[placeholder*="填写标题"]`，原生 setter + input/change 事件：
  `Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(inp, v)`。

### 2.4 填正文（ProseMirror，老坑最多的一步）
- 正文文件统一存 **base64**（`xxx.b64`），shell 里 `B64=$(cat xxx.b64)` 直接用。
  ⚠️ **双重编码坑**：`.b64` 文件本身就是 base64，**不要**再 `base64 -i` 包一层。
  页面内解码用 `decodeURIComponent(escape(atob('$B64')))`。
- **写入顺序**：`ed.focus()` → Range.selectNodeContents + removeAllRanges/addRange 全选 →
  `document.execCommand('delete')` **先删空** → 再 dispatch ClipboardEvent('paste')。
  ⚠️ paste 只会**追加**、不会替换选中内容（2026-10-29 实测推翻了旧的「全选后直接 paste 可替换」记录）；
  不先删空，正文会变成旧文+新文拼接。
- 写完回读：`ed.textContent.length` 和 head/tail 各 12 字符，与源文核对。
- 正文含 #话题 时只是纯文本；真话题转换需逐个点击下拉项，时间紧可跳过（纯文本标签仍可搜索）。

### 2.5 定时发布设置
- 开关：`.post-time-wrapper .d-switch`，真实坐标点击。
- ⚠️ **状态判断不要用 `className.includes('d-in')`**——它会误匹配 `d-inline-block`（2026-10-29 踩过，
  实际是关的被误判成开）。判断开着没有：截图看视觉状态，或匹配 `/\bd-in\b/`。
- 时间输入框：`.post-time-wrapper input` 中 value 匹配 `\d{4}-` 的那个，原生 setter 写
  `YYYY-MM-DD HH:mm` → input + change → **sleep 1.5s 后回读**（Vue 受控渲染有延迟）。

### 2.6 点发布按钮（全程最危险的一步）
- 按钮在 `<xhs-publish-btn>` 的**封闭 shadow DOM** 里，snapshot 抓不到、文本检索无效，只能真实坐标点。
- 三条铁律（违反任一都会**静默失效**：无报错、无请求、页面不动）：
  1. 用 `dragTo='{"x":X,"y":Y}'`（在坐标做真实 press+release），不用 eval click；
  2. 坐标**点击前一刻**才取：eval 读 host rect + `elementFromPoint` 探测，禁止读完 sleep 再点；
  3. host 矩形内**中心是空白**（「暂存离开」和主按钮之间），主按钮在 host **右侧区域**——
     用 `elementFromPoint` 逐一探测 host 宽度内几个点，找命中的真实元素再点。
- ⚠️ 别点左侧菜单的 `span.btn-text`「发布笔记」——会导航到新发布页，**已填内容全部丢失**。

## 3. 发布前终检（一次截图 + 一次 eval）

- 截图整页：核对视频已转码、主封面正确、标题、正文长度、定时开关开、时间正确。
- eval 读 `xhs-publish-btn`：`submit-text` 应为「定时发布」（定时模式）、`submit-disabled` 非真。

## 4. 成功判据（缺一不可，与 browser-execution.md §2 对齐）

1. 点发布后 URL 跳转（`?source=&published=true` 或回 note-manager）
2. 笔记管理页**页内文本**同时出现：笔记标题 + 「审核中/定时发布」+ 定时时间
   （eval/read_text 页内搜，只看 URL 不算）
3. 回读到的标题、定时时间与设置值一致

失败或连续两次异常 → 按 browser-execution.md §5 停止并记录，不硬试。

## 5. 坑清单（新 agent 发布前必读）

| 日期 | 坑 | 正确做法 |
|---|---|---|
| 10-25 | ProseMirror `execCommand('selectAll')` 选不中 | Range.selectNodeContents + removeAllRanges/addRange |
| 10-25 | 编辑页后台标签被 Chrome 限流，`type` 反复 PAGE_BUSY | 写值走原生 setter + 事件，别跟 type 死磕 |
| 10-27 | `<xhs-publish-btn>` 封闭 shadow DOM，文本检索/.click() 全无效 | 真实坐标 dragTo，坐标前一刻实时取 |
| 10-27 | 点 host 中心静默失效（容器内两按钮之间是空白） | elementFromPoint 探测真实按钮位置再点 |
| 10-27 | 点错左菜单「发布笔记」丢失全部已填内容 | 发布前确认目标不是菜单项 |
| 10-27 | `upload` 一次只能一个文件，逗号分隔 ENOENT | 逐张投递 |
| 10-29 | `upload` 工具没有 `files=` 参数 | 位置参数 `path= tabId=` |
| 10-29 | eval 注入 DataTransfer 设封面无效（Vue 不理合成事件） | 真实点「上传」钮 → 立刻 upload(path) |
| 10-29 | 主封面「编辑封面」按钮默认隐藏 | 先 click 槽位触发 hover，再点 `.cover-edit-entry` |
| 10-29 | PK 槽 3/3 被推荐占满、删除按钮被画布挡住 | 关 PK 开关走主封面，不恋战 |
| 10-29 | `.b64` 文件被再次 base64 → 双重编码，正文变乱码 | `cat xxx.b64` 直接用 |
| 10-29 | paste 追加不替换（推翻旧记录） | 先 execCommand('delete') 删空再 paste |
| 10-29 | `includes('d-in')` 误匹配 `d-inline-block` | `/\bd-in\b/` 或截图目视 |
| 10-29 | 视频偶发「上传失败 网络错误」 | 点重新上传 + 重投，20s 后复查 |
| 10-28 | TTS 全角冒号插 ~8s 停顿（旁白稿阶段） | 旁白一律用逗号，不用冒号 |
| 10-30 | 封闭 shadow DOM 下 `elementFromPoint` 对 `<xhs-publish-btn>` 全程返回 host 本体，无法用「命中=按钮」区分空白与按钮 | 改用截图定位：`screenshot savePath` + `sips` 读图片像素，换算 CSS 坐标（图片px ÷ 1.2 = CSS px，DPR2×60% 缩放），再 `click x= y= real=true` 纯坐标点击 |
| 10-30 | `click selector=xhs-publish-btn dragTo='{"x":..}'` 会把 press 落在 host 中心空白，**静默失效** | dragTo 不要与 selector 连用；坐标点击时不传 selector |
| 10-30 | 成功页 HTML 里的 24 位 hex 是 CDN 图片 hash，会被误当 noteId | noteId 取 note-manager 卡片 `data-impression` 属性 JSON 的 `noteTarget.value.noteId` |
| 10-31 | **定时发布有最短提前量 ≈ 60 分钟**：18:55 写「19:30」回读变 19:55/19:56（自动跳到最早可定时时刻，且随当前时间漂移） | 定时时间早于「当前时间+60min」就别走原生定时，直接在窗口内实发；或提前 ≥70 分钟完成填表再写定时时间 |
| 10-31 | 主封面槽 `.cover-edit-entry` 的 hover 由 `.cover .operator` 层控制，真实 click/dragTo 移入槽位后 `getBoundingClientRect` 仍为 0（CSS :hover 未生效） | eval 直接 `entry.closest('.operator').style.display='flex'` 强制显示，取实时坐标 `click real=true`（10-31 实测「编辑封面」在 455,462；封面 modal 内「完成」按钮别按截图估，用 DOM 定位，实测在 1298,672） |

## 6. 置顶首评（上线后执行）

### 7.1 发首评（web 可全程完成，2026-10-29 跑通）

1. **取笔记链接**：裸链接会 300031，必须带 token。可靠来源是**搜索页**：
   开 `www.xiaohongshu.com/search_result?keyword=<完整标题>`，结果卡 `a[href*="/search_result/"]`
   的 href 自带 xsec_token（主页 user_posted 在刚上线的十几分钟内可能还没有这条，别死等）。
2. **打开详情**：eval `location.href='/search_result/<noteId>?xsec_token=<TOKEN>&xsec_source='`，
   判据看正文是否渲染（同 300031 三步法）。
3. **写评论**：
   - snapshot 找输入框（`.content-input`）和「发送」按钮的 ref；
   - ⚠️ **type/click 对 `.content-input` 会报 NOT_INTERACTABLE**——中心点被 placeholder 的
     SPAN 挡住，工具级点击一律被拦；
   - 正确做法：eval `document.querySelector('.content-input').focus()` 后
     `document.execCommand('insertText', false, text)`（走真实输入事件，Vue 能收到，实测 len 精确）。
4. **发送**：⚠️ 发送按钮中心同样被内部 span 挡住，click x/y 也报 NOT_INTERACTABLE。
   eval 派发完整鼠标事件序列可触发：
   `['pointerdown','mousedown','pointerup','mouseup','click'].forEach(t=>btn.dispatchEvent(new E(t,opts)))`，
   opts 带 clientX/clientY（按钮中心）。
5. **验证**：评论计数 +1（截图）+ 列表首条可见文本 + 作者标识。⚠️ **发送成功后输入框可能不清空**
   （实测 bug）——必须手动清空（focus + selectNodeContents + execCommand('delete')），否则易误发重复评论。
   验证不重复：计数只 +1、列表里目标文本只出现一次。

### 7.2 置顶（只能手机 App，2026-10-29 确认）

- 网页版自己评论的「···」菜单里**只有「删除评论」，没有置顶**；创作者后台 comment/manage 已 404。
- 置顶操作：**手机 App → 笔记 → 长按目标评论 → 置顶**。发完首评后在汇报里明确请用户在手机上点一下。

## 7. 图文发布

流程同 `browser-execution.md` §3 图文状态机；封面/正文坑参照 §5。
（图文首次跑通后按同样格式把分步要点补进 §2。）
