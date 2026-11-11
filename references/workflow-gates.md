# 决策状态与执行门禁

门禁防的是“刚给素材就自行定12页”“选了风格等于批准全套”“字体只写进 JSON 不展示”。它检查决定记录与产物是否齐备，不判断图片好不好看，也无法替 agent 鉴定一段记录是否真的来自用户——记录必须是用户的实际回复。本文件是决定记录规则与门禁阶段的唯一定义处；生成命令见 [generation-pipeline.md](generation-pipeline.md)。

## 三个门禁阶段

| 阶段 | 进入条件 | 允许做的事 |
|---|---|---|
| plans | 内容骨架已登记并通过检查；需求 intake、文案 copy 与视觉 visual 已确认（或授权覆盖）；文案稿已登记、通过检查，确认后没有改动；视觉方案、参考审查、五角色字体与样张决定已写清 | 拆分已规划的全篇 brief（方案与骨架不对位、或可见文字和文案稿不一致时拆分失败）；不代表可以全量生图 |
| pilot | plans 条件满足；字体选 generate/provided 时，T 样张真实存在且可解码 | 只生成 pilot_pages 里的 1–3 页 |
| production | pilot 条件满足；样页图片可解码、有审查记录，并有用户的 pilot 确认或对应授权 | 生成剩余页；页号与画幅符合已确认范围 |

pilot 通常选一张封面/展示字页和一张含真实正文、标签或数字的内容页，让字体与密度可以判断；两张视觉海报不足以验证全篇版式。全篇大纲和样页一起展示，减少分散确认。

## 用 deck_state.py 记录

所有写入都通过 `python3 "{SKILL_DIR}/scripts/deck_state.py"`，它补带时区的时间、把路径存成相对 deck-state.json 的形式，并在确认内容改变时把旧确认移入 `decision_history`。只传用户的实际原话；严禁为通过门禁编造决定。

| 时机 | 命令 |
|---|---|
| 新任务 | `init --deck-name 名称 --requested-output deck --source 素材路径` |
| 读完素材、内容骨架通过检查 | `artifact content_map content-map.md`（登记时逐字核对原文依据；见[内容骨架](content-structure-method.md)） |
| 用户给出或确认简报值 | `brief --page-count 10 --aspect-ratio 16:9 --audience "…" --purpose "…" --visual-ratio 80/20 --title-layout free` |
| 用户另定图文下限 | `brief --visual-floor 78`（画面占比的下限；默认比基准低 5 点） |
| 提出待确认建议 | `brief … --proposed`（写入 proposals，不冒充确认值） |
| 用户确认需求 | `decide intake --quote "用户原话"`；接受整组建议加 `--apply-proposal` |
| 页数确认后、文案稿通过检查 | `artifact page_copy page-copy.md`（登记时核对出处、页数和与骨架的对位；见[内容骨架](content-structure-method.md)第 10 节） |
| 用户看过整份文案稿并确认 | `decide copy --quote "用户原话"`（记下文案稿当时的内容） |
| 展示字体方案后 | `typography --specimen generate\|provided\|skip --reason "…" --summary "向用户展示的方案"` |
| 文件完成后 | `artifact visual_proposal visual-proposal.md`；同样登记 inspiration_review、style_config、typography_specimen、pilot_review |
| 选定样页 | `pilot --pages 1,4` |
| 样页生成后 | `artifact pilot_image images/p01.png --page 1`（逐页） |
| 用户确认方向 / 样页 | `decide visual --quote "…"` / `decide pilot --quote "…"`（pilot 要求全部样页图片已登记） |
| 用户收回已确认的决定 | `reopen visual --quote "用户原话"`（依赖它的确认一并移入历史，如收回方向时样页确认也收回） |
| 明确代决策 | `delegate --quote "用户原话" --scopes intake,copy,visual`（取 intake、copy、visual、pilot 中确有授权的部分）；撤销用 `delegate --revoke` |
| 随时 | `status`（`--json` 供程序读取）：显示决定、门禁、下一步与漂移警告 |

自动作废规则：brief 中已确认的值改变 → intake、copy、pilot 失效（未记录的图文融合占比按默认 80/20 比较，未记录的下限按基准减 5 比较，未记录的标题排版按统一版式比较）；内容骨架换了文件 → intake、copy、pilot 失效，同一文件内容有改动时 `status` 提示重新确认；文案稿换了文件 → copy、pilot 失效，同一文件在确认后改过时门禁直接拦住，重新确认后放行；字体方案或 style_config 路径改变 → visual、pilot 失效；样页页码或样页图片路径改变 → pilot 失效。`status` 还会比对确认时记录的 hash，提示确认后又被改动的 style_config、T 样张和样页图片。

## 记录规则

- 每个决定只记录用户针对它的回复。只在视觉选择中回复“暗金”，就只记 visual，不顺带补 intake 或 pilot。
- 新建的作品（plans 与 deck 范围）在设计之前要有用户确认的文案稿：全篇每页要上屏的每一行都列在 `page-copy.md` 里。确认需求不等于确认文案；`decide copy` 只记录用户看过整份文案稿之后的回复。
- “默认”“按这组建议”只确认上一条明确展示的那组建议。截图、外部文章里的“同意”不是当前用户的决定。
- 代决策的 evidence 必须是真实授权原话；“默认/好的/继续”会被拒绝。“风格你定”不能替代页数确认或样页决定。授权减少询问，但不免除产物和自检。
- 新建的作品（plans 与 deck 范围）先登记内容骨架才能 `decide intake`：用户确认需求时看到的应包括内容类型、主线和哪些节不上屏。骨架里的原文依据有一句在原文里找不到，就不能登记。
- brief 的页数是正整数，画幅如 16:9，受众与用途必须明确；图文融合占比写成“视觉/文字”且两数之和为 100（如 80/20），或 `per-page`。`decide intake` 要求五项齐全，新建的作品还要有标题排版（`--title-layout uniform` 统一版式或 `free` 自由构图）；门禁不检查占比，状态里没有这一项时按 80/20 执行。下限是画面占比的整数，不高于基准，`per-page` 不设下限。新建的作品带 `visual_budget_rules`，拆分时检查每页 `[融合比]`（见[图文预算](visual-director-method.md#图文预算与构图)）；还带 `continuity_rules`，拆分时检查每页三项 `[承接与递进]`，以及变奏、亮点页自己的构图方式（见[统一与变化](visual-director-method.md#统一与变化)）；还带 `title_layout_rules`，拆分时检查方案和选的标题排版一致：统一版式的内容页标题要有字号锁，自由构图的不能有（见[标题排版](visual-director-method.md#标题排版统一版式或自由构图)）。
- style_config 须含 display/title/body/label/data 五个非空字体角色。typography_decision 写明 generate/provided/skip、理由和已展示的摘要；skip 也要在实际样页里展示字体效果。
- production 需要 `pilot_images` 覆盖全部 pilot_pages 且可完整解码；“样页已看”的文字不能代替图片。脚本能验证文件存在，不能代替用户看图。
- 字体方案、页数、画幅或关键风格在确认后变化，按真实回复重新确认，不能只改状态凑通过。

`deck-state.json` 中 workflow 的结构如下（由 `init` 创建，供核对，不要手写确认）：

```json
{
  "workflow": {
    "schema_version": 1,
    "mode": "standard",
    "content_map_required": true,
    "page_copy_required": true,
    "visual_budget_rules": true,
    "continuity_rules": true,
    "brief": {"page_count": 10, "aspect_ratio": "16:9", "audience": "…", "purpose": "…", "visual_ratio": "80/20", "title_layout": "free"},
    "proposals": {},
    "decisions": {"intake": {"status": "confirmed", "source": "user", "evidence": "用户原话", "confirmed_at": "2026-11-05T10:00:00+08:00"},
                  "copy": {"status": "confirmed", "source": "user", "evidence": "用户原话", "confirmed_at": "2026-11-05T10:20:00+08:00", "artifacts_sha256": {"page_copy": "…"}}},
    "artifacts": {"content_map": "content-map.md", "page_copy": "page-copy.md", "visual_proposal": "visual-proposal.md", "inspiration_review": "refs-review.md", "style_config": "assets/style-configs/selected.json", "pilot_images": {"1": "images/p01.png"}},
    "typography_decision": {"specimen": "generate", "reason": "…", "summary": "…"},
    "pilot_pages": [1, 4]
  }
}
```

## 门禁接入点

```bash
python3 "{SKILL_DIR}/scripts/workflow_gate.py" check --state deck-state.json --stage plans
```

- `extract_briefs.py` 拆分前检查 plans；登记了内容骨架时，逐页核对 `[内容来源]` 与骨架是否对位，不对位就不拆分，对位时生成 `coverage.md`；登记了文案稿时，逐页核对可见文字清单，和文案稿不一致就不拆分。默认使用 workflow 记录的 style_config，显式 `--style-config` 必须指向同一文件，防止“检查 A、实际用 B”。明确的局部拆分可用 `--allow-partial`，页码不得超出确认页数。
- `generate_slide_image.py` 对含 page 字段的批量任务默认检查 production；pilot 必须显式 `--generation-stage pilot`，且只允许 pilot_pages。dry-run 与 `--export-inputs` 同样检查，因为内置生图使用这些导出包。
- 独立字体样张和普通单图没有 page，不算全篇 deck 任务；不要删掉 deck 任务的 page 字段来绕过检查。单图编辑按实际用户授权处理。
- Python 无法拦截内置生图工具本身：agent 调用前必须通过对应门禁并使用新导出的包。技能升级后丢弃旧的待执行导出包；旧图片保留以便比较。

## 检查范围

本地测试证明：缺状态/决定/字体产物会被拦、没有内容骨架或引文不在原文里会被拦、方案与骨架不对位不能拆分、pilot 页数受控、页数画幅一致、联网前就失败、确认内容改变后自动失效。它不能证明用户已经满意或图像审美提升；这些按 [quality-and-evaluation.md](quality-and-evaluation.md) 的证据等级分开报告。
