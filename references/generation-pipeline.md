# 拆分、生成、恢复与交付

命令在 deck 工作目录执行；{SKILL_DIR} 指技能目录，路径含空格时加引号。Python 3.9+，先安装 requirements.txt 或使用平台已有的 Pillow/python-pptx 运行时。所有 --help 可离线查看。

生成前用 `deck_state.py` 记录真实决定并通过对应门禁（规则见[workflow-gates.md](workflow-gates.md)）。拆分、含 page 的批量任务以及 dry-run/native 导出都会检查 deck-state.json，缺决定或字体产物时在写产物/提交前停止。

## 渠道与能力

内置工具优先服从用户已选渠道。外部脚本保留 gpt-image-2（ZenMux、默认 openai/gpt-image-2）、seedream（Ark、默认 doubao-seedream-5-0-pro-260628）与 apiyi（APIYI 中转、OpenAI Images 协议、默认 gpt-image-2.6-sunburst、画质 high，可用 APIYI_IMAGE_QUALITY 覆盖）适配器。具体参数见脚本 CHANNELS 和 configured_capabilities；这些是配置，不是实时能力证明。

默认模型名会随服务方更新而过期：单次用 --model 覆盖，长期用环境变量 ZENMUX_IMAGE_MODEL / ARK_IMAGE_MODEL / APIYI_IMAGE_MODEL 覆盖（优先级：任务 model → --model → 环境变量 → `configure.py set-model` → 内置默认）。覆盖前先核对实际提供方支持；模型上线不等于所有代理可用。每次状态记录 channel/model/size/pipeline version。内置模型身份不公开则记 platform-managed；不冒称 Image 2.5。现有渠道不会因为升级 skill 自动切换。

密钥读取 --api-key → 环境变量 ZENMUX_API_KEY / ARK_API_KEY / APIYI_API_KEY → `configure.py set-key` 保存的本机文件 → macOS Keychain zenmux_api_key / ark_api_key / apiyi_api_key。密钥只存在用户自己的配置目录，不写入技能目录、作品目录、报告或方案；dry-run不读密钥。请求被拒绝时命令行会显示服务方给出的原因（如无权限、模型不存在），运行记录里只留状态码。配置方法见[首次配置](setup.md)。

## 拆分与参考

```bash
python3 "{SKILL_DIR}/scripts/extract_briefs.py" design-plans.md --out-dir . --workflow-state deck-state.json --style-config assets/style-configs/my-style.json
```

产出 deck-constants.txt、briefs/pXX.txt、manifest-anchors.json、manifest-pages.json。登记了内容骨架时，拆分先逐页核对 `[内容来源]`：有页没写来源、有节既没上屏也没标“不上屏”、同一部分的页不连续、顺序和原文相反、目录或章节页不是原文的章名，都会失败并逐条列出；通过时另外写出 `coverage.md`（覆盖表），④给用户看。登记了文案稿时，再逐页核对可见文字清单：比已确认的文案稿多一行、少一行，或 `[内容来源]` 不同，都会失败并逐条列出。新建的作品还会检查每页 `[融合比]`：没写、低于下限、低于基准没写原因、达到基准的页不足三分之二（封面、章节页、结尾页不计），都会失败并列出页码；通过后每页的比例写进 brief 末尾（封面、章节页、结尾页不加），报告里的 `visual_budget` 列出低于基准的页。常量块里声明的字号锁会按字数算成每行的字框写进 brief；锁定的一行在锁定字号下放不进“最宽”时拆分失败，先改短或拆成主副标题。完整页序从1连续且与已确认页数一致；缺常量/brief/清单、重复页号、无效族锚、静态文件缺失先失败，不覆盖旧产物。--allow-partial只用于明确的部分方案，不能超出确认页数；全篇交付依然要求完整页序。未传--style-config时采用workflow.artifacts.style_config，显式传入须为同一文件。

字段：
```markdown
**[内容来源]**：C2
**[场景族]**：编辑内容族（锚定页 P1）
**[版式锚]**：[L-content] assets/layout/content.png
**[字体参考]**：[T-title] assets/typography/title.png
**[参考图]**：[C1] assets/brand/logo.png
```
族锚页写“本页为族锚定页”。T路径可省略，从 typography.references 按role解析。多项用分号分隔；路径有空格时用引号。保持一页每个字体角色一张有效样张。模板整图不传生图。

页面可选 `**[节奏]**：稳定｜变奏（只变：…）｜亮点（…）`，省略为稳定。风格配置有 `styleBoardImage` 时，变奏与亮点页的 family 引用换成 `[S-board]`，因此不依赖族锚图片先生成；没有风格板时照常附族锚。风格板本身是不带 page 字段的普通单图，以已确认的族锚定页为参考生成，文件存在后再写进配置并重新拆分。

S/family 与 L 是否附图按权重决定（规则见[style-config-schema.md](style-config-schema.md)）；styleWeight<70 时不附 S 或族锚图片，即使未指定 --single-phase 也没有 family 图片依赖。T 不受权重影响，referencePolicy.mode=off 时禁用。禁用某类参考后，brief中仍点名对应[S]/[L]/[T-role]/[family]会在拆分阶段明确报错；应将依赖图的语句改成独立文字规范，或显式启用并分配参考，不静默删文案。

manifest仍是jobs数组，例：
```json
[{"page":2,"prompt_file":"briefs/p02.txt","output":"images/p02.png","references":[
  {"path":"images/p01.png","role":"family","id":"family"},
  {"path":"assets/layout/content.png","role":"layout","id":"L-content"},
  {"path":"assets/typography/title.png","role":"typography","id":"T-title","typography_role":"title"},
  {"path":"assets/brand/logo.png","role":"brand","id":"C1"}
]}]
```
只写路径的字符串引用按未定角色处理，不能据此断言风格或版式。brief用稳定ID；实际图片排序和编号由deck_runtime生成，不手写“参考图1/2”。手写图号的prompt需要先改成ID再预检。

--style-ref 可补充S，--style-ref-scope all用于确有需要的强传导（例如角色设定图要到达每一页）；此时变奏与亮点页只附 S、不附族锚。--single-phase额外输出manifest-all.json，适合已统一配置的简单deck；按真实依赖选择，不强制所有任务两阶段。两阶段时，C的族锚文件在B完成后才存在；先预检B，再预检C。

## 预检与执行

```bash
python3 "{SKILL_DIR}/scripts/generate_slide_image.py" --batch manifest-pilot.json --generation-stage pilot --prefix-file deck-constants.txt --aspect-ratio 16:9 --dry-run --export-inputs native-pilot.json
python3 "{SKILL_DIR}/scripts/generate_slide_image.py" --batch manifest-pilot.json --generation-stage pilot --prefix-file deck-constants.txt --aspect-ratio 16:9 --workers 2
# 全篇大纲、实际样页及字体经用户确认或授权范围内完成自检后：
python3 "{SKILL_DIR}/scripts/generate_slide_image.py" --batch manifest-pages.json --prefix-file deck-constants.txt --aspect-ratio 16:9 --dry-run --export-inputs native-pages.json
python3 "{SKILL_DIR}/scripts/generate_slide_image.py" --batch manifest-pages.json --prefix-file deck-constants.txt --aspect-ratio 16:9 --workers 4
```

例中16:9必须替换为已确认画幅。manifest-pilot.json从机械拆分产物按workflow.pilot_pages筛出，不重新手写参考绑定；剩余批次需覆盖所有未生成页，包括未在pilot中的族锚，先处理实际依赖。anchors只是依赖分组，不是自动获准生成的页集。外部运行追加已选--channel/--model。并发按服务实际限制，默认4、上限8；--size也须满足已确认比例。

内置生成先运行通过对应workflow门禁的dry-run，取resolved_prompt及references顺序逐页调用工具。技能更新或决定变更后，已导出的包须重新预检，不能拿缓存包绕过新决定。工具尺寸能力不同则先解决与用户画幅的兼容并记录实际尺寸，不能把外部adapter快照说成内置模型证明。保存结果后运行deck_delivery检查；不能重写角色映射或遗漏常量。

## 字号锁：测量、样板与重画

首张样页前可先用 `text_lock.py guide` 制作同画幅的原生几何样板，写到字号锁行的 `样板 路径`，拆分时自动作为 `[SZn]` 附给该组各页。它只给几何，不替代生图或用户验收。完整用法见 [字号控制](title-size-control.md)。

逐页可选 `**[编辑底图]**：[target] 路径`。拆分后自动写 `edit: true`、`target_image`，普通和单阶段 manifest 都保留该语义；仍按 pilot/production 门禁预检，内置工具仍使用新导出的 resolved_prompt 和 references。不要把带 target 的编辑任务手工改成全页新生成，也不要覆盖底图。

方案声明了字号锁时（写法见[空间定位图](visual-director-method.md)），拆分出的 brief 末尾带着每一行的字框，任务清单带着 `text_locks`。拆分前先过两道检查：一行在锁定字号下放不进“最宽”，或同组字数不统一（六个字以内每页相同，更长的最多差 2 个字，另有 `字数 a–b` 的按它），拆分都会失败。

**测量只出报告，不触发重画。** 每批图生成后在本机量一次（不花钱），把结果和总览图一起给用户看：

```bash
python3 "{SKILL_DIR}/scripts/text_lock.py" check --manifest manifest-anchors.json --manifest manifest-pages.json --report text-lock-report.json
```

- 输出每一行的实测字高、位置与偏差，以及每个分组跨页的字高范围。默认容差：字高 ±4%，位置 ±3%（画布边长）；`--tolerance-height`、`--tolerance-position` 可调。全部在容差内返回 0，图片还没生成的页报 `missing`。
- **重画哪几页由用户看图决定。** 重画要花钱，测量只是参考：超差但用户看着可以的页不重画，用户觉得不对的页即使在容差内也可以重画。不要自动“测量 → 重画”循环；授权代决策时也一样，把测量结果列给用户，不自行重画。
- 用户选定后，只给这些页记纠正说明，再拆分、再生成：

```bash
python3 "{SKILL_DIR}/scripts/text_lock.py" check --manifest manifest-anchors.json --manifest manifest-pages.json --feedback text-lock-feedback.json --pages 6,9
```

  `--feedback` 必须带 `--pages`（用户说全部重画时写 `--pages all`）。重新运行 `extract_briefs.py` 后，这些页的 brief 末尾会加上“上次字画大了 14%”这样的说明；再运行同一条生图命令，只有 brief 变了的页重新提交，其余复用。重画会覆盖同名图片，想保留上一版先另存。量过关的页，它的说明留在反馈文件里不动，brief 不变就不会再被重画；不要手工删改反馈文件。
- 用户决定不重画的超差页，在 review-notes 该页写 `"text_lock_accepted": {"by": "user", "note": "用户原话"}`；没有这条记录，交付检查不放行。
- 量不出来的行（`unlocated`：字压在画面上、底色不干净，或这一行没有画出来）不算通过。看图核对后在 review-notes 该页的 checks 里加 `text_lock`。

**字号样板（样页确认后、生产前）。** 请用户在样页里指定每组字号最对的一页，从那一页剪出样板：

```bash
python3 "{SKILL_DIR}/scripts/text_lock.py" template --manifest manifest-anchors.json --manifest manifest-pages.json --page 11 --group 标题 --out assets/typography/size-title.png
python3 "{SKILL_DIR}/scripts/text_lock.py" template --manifest manifest-anchors.json --manifest manifest-pages.json --page 10 --group 章节小标题 --group 章节主标题 --out assets/typography/size-chapter.png
```

- 样板是同尺寸的纯色画布，只留这几行字（含描边、厚度和投影），旁边的画面去掉。字要在干净的底色上才剪得出来。
- 按输出的提示，在常量块对应的字号锁行末尾加 `，样板 路径`；指定页和原锁定值不同时，输出会给出改后的锁定值，以这一页为准改掉。重新拆分后，用这组字的每一页都附上样板（[SZ1]、[SZ2]…）。拆分报告里“还没有字号样板”的提醒消失。
- 在生产前加。生产中途加样板，这组已经出图的页参考图都变了，再跑整批会全部重画；只想补后面的页，就只跑那些页的清单。
- 已有的样板文件不会被覆盖，要换时加 `--overwrite`，同样会让用它的页全部变化。

这个工具不改幻灯片，也不调用生图；`template` 只是从用户选定的页另存一张参考图。

## 字体样张

```bash
python3 "{SKILL_DIR}/scripts/typography_specimen.py" --config assets/style-configs/my-style.json --role title --text "本套需要的标题文字" --output-dir assets/typography
```

该命令仅准备prompt/jobs/specimen元数据，不生成图片。样张请求不拼入deck常量，避免把背景/场景带进纯字体图。使用已选生图工具生成后，核字、缩小看笔画并检查纯字隔离，通过后将图片登记到typography.references。不要把计划路径提前当成已验证引用。

## 修复

```bash
python3 "{SKILL_DIR}/scripts/generate_slide_image.py" --edit --target-image images/p03.png --reference '{"path":"assets/typography/title.png","role":"typography","id":"T-title","typography_role":"title"}' --prompt "仅调整标题字形到[T-title]，保留本页准确文案、正文、位置与颜色。" --output images/p03_v2.png
```

target必须是待修页，输出不得覆盖target或任何参考。改字号/风格时，可加各自角色的合格参考；去掉参考后编号自动重建。重新生成可用修改后的brief，不需target；二者要明确区分。

## 恢复与版本

默认run-state.json，--state-file可改。记录输入hash（包含文案、引用字节、尺寸、模型、渠道）和实际结果：
- success + 输入未变 + 图片验证通过：复用，不提交。
- accepted：只恢复已返回图片的下载/验证。
- failed（明确拒绝）：--retry-failed重试。
- submitting/unknown：可能已提交，不自动重付费；核对服务状态后，明确需要新请求时才用--force。
- 同一页、同样的输入从第二次没有回音起，脚本会报出次数。先告诉用户这一页已经发了几次、是否扣费不明，由用户决定要不要继续。继续时一页一页单独发；原样重发有可能成功，只有这一页反复失败时，先减轻它的请求再发：少附一张参考图（比如同族的锚定页，构图已经写在这一页的文字里）。原因看不到时不替它下结论。
- --force：明确创建新请求，不等于普通恢复。
- 新文案或引用变化：只重做相关页。旧图无运行记录时用新输出路径，避免误覆盖。

不对超时不明的POST盲目重试。输出先验证后原子替换。运行状态不是用户确认记录，deck-state仍保存阶段与选择。

## 检查与装配

```bash
python3 "{SKILL_DIR}/scripts/deck_delivery.py" --manifest manifest-anchors.json --manifest manifest-pages.json --aspect-ratio 16:9 --expected-pages 8 --contact-sheet overview.png --report quality-report.json
python3 "{SKILL_DIR}/scripts/deck_delivery.py" --manifest manifest-anchors.json --manifest manifest-pages.json --aspect-ratio 16:9 --review-notes review-notes.json --output-pptx my-deck.pptx
```

review-notes的格式见quality-and-evaluation。带字号锁的页会被重新测量：超差且没有接受记录时报 `text_lock_drift`，不装配。结构不合格不装配，视觉检查未完成时报告pending；不能把生成了PPTX当成已经验收。装配重开验证每页一张满铺图、无覆盖文字、图像顺序与内容hash一致。
