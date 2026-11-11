# 收藏风格，供下次复用

这是用户的本地设计资产库。保存已选设计描述、配色、字体角色、布局规则与参考图副本，复用时根据新内容重做页面。它不是原生PowerPoint母版，也不是把旧PPT换几个字。

## 完成后的唯一询问

默认自动提示只在整套PPTX交付且必要检查完成后发生；不能在只完成方案、失败页未修或review pending时提前推荐为“已验证模板”。先准备可审阅的名称、简短描述、拟保留规则、参考图缩略图和保存范围，再询问：

> 要把这套PPT的风格、设计规则和参考图存成「暖白编辑风」，方便下次复用吗？

建议选项：保存为此名称 / 换个名字保存 / 这次不保存。名称可按已选视觉特征拟定，用户可以直接改名。不要让用户重新填写字体、配色等已有信息。

- 明确说过“做好后保存”“这个风格以后复用”：完成后按授权执行并报告名称/版本，不再次询问。直接的模板保存/管理请求按其范围执行，不强行先做一套PPT。
- “挺好看”“都你决定”“快速做完”只代表设计或制作意见，不自动等于同意长期收藏。没有答复不保存、不定时追问，也不阻塞已经完成的PPT交付。
- 已询问、已拒绝或已保存，同一deck恢复、重新展示、普通改字后不重复询问。用户主动要求另存/更新时直接响应；同名保存新增版本。
- “这次不保存”只对当前deck有效；“以后别再问”才记录全局提示偏好。“以后都保存”仅按真实授权范围执行，不自行扩大到其他品牌资产。

用 `deck_state.py template-offer` 记录状态（not_offered/offered/declined/approved/saved）：发问前先记 `offered --candidate-file template-candidate.json`，已询问、拒绝或保存过时该命令会拒绝重复询问，用户主动要求时加 `--user-requested`；用户回复后记 `declined` 或 `approved --quote "用户原话"`；保存成功后记 `saved --template-id … --version …`。保存失败保持 approved，不能记 saved。普通代码或文案修订不重置状态。

## 提炼与保存范围

从用户明确喜欢的最终设计提炼 `design_notes`：视觉气质、字形与层级、留白、页型骨架、图表规则、使用场景、允许变化与用户不喜欢的处理。单页修改不随意升级为全篇规则。把已验证的style-config复制成候选配置后再清理，不改原项目配置。

默认仅保留已选S风格图、L完整页面参考、T角色样张，以及用户同意留下的少量成品预览；用role与note注明用途和来源。参考中可见的旧文字仅示意，不是未来逐字稿。字体sampleText在归档时移除；下一套从新素材产生实际文字。

不递归打包工作区，不携带原稿、全篇逐字稿、旧页序、生成运行状态或凭据。客户Logo/产品/人物/二维码等C资产不作为通用模板默认内容。只有用户明确选择并列入候选时才保存；新任务仍须根据当前品牌决定是否启用，不能因库里有文件就自动采用。脚本校验文件与引用，语义提炼仍由agent负责。

apply将明确的旧品牌身份块移出新style.json，放入template.json的available_optional_brand_config；品牌图片列在available_optional_brand_assets。两者是待明确选用的资料，不是新项目默认内容。通用配色保留，遇到新品牌色要求时按当前任务覆盖。

## 候选文件

在deck根目录准备template-candidate.json。其style_config、assets.path及配置内的图片相对路径均从候选文件所在目录解析，与原deck执行cwd保持一致。绝对本地路径可在保存时读取，归档后全部改为库内相对路径。只接受本地可解码图片，已过期临时链接先取得实际文件。

```json
{
  "schema_version": 1,
  "name": "暖白编辑风",
  "description": "暖白底、深色方整标题、清晰正文与宽松图解",
  "tags": ["编辑排版", "知识分享"],
  "design_notes": "标题与正文保持明确层级；比较页沿用同一骨架。新内容决定页数、数字与可见文案。",
  "style_config": "assets/style-configs/template-style.json",
  "assets": [
    {"id":"S1","role":"style","path":"assets/style/selected.png","note":"只复用视觉语言，不复制样本文字"},
    {"id":"T-title","role":"typography","path":"assets/typography/title.png","note":"只约束title字形"}
  ]
}
```

这只是结构示例，必须换成当前真实文件。配置中的S/L/T和referenceAssignments图片须全部列入assets；不想保存的引用先从候选配置移除或转为文字规则，避免失效绝对路径。只有实际用户同意后才加入approval：status=approved、by=user、note=真实决定原话、approved_at=带时区的实际ISO时间。示例、沉默与agent自评不可伪造成审批记录。

## 本地命令

默认库为 `${XDG_DATA_HOME:-~/.local/share}/moyu-ppt/templates`，与技能安装目录及单套PPT分开；升级skill不会覆盖收藏。也可传--library指定用户数据位置。脚本只操作本地文件，不联网、不调用生图。

save的原子版本锁使用fcntl，当前支持macOS/Linux；其他平台明确报不支持，不假装保存成功。list/show/apply不依赖该锁。库正被写入时立即报告busy，结束后可重试。

```bash
# 可先预检候选，无需approval，不创建库文件
python3 "{SKILL_DIR}/scripts/template_library.py" save --candidate template-candidate.json --dry-run
# 用户已确认，候选中已记录真实approval
python3 "{SKILL_DIR}/scripts/template_library.py" save --candidate template-candidate.json
python3 "{SKILL_DIR}/scripts/template_library.py" list --query "编辑"
python3 "{SKILL_DIR}/scripts/template_library.py" show --id "实际返回的id"
# 在新deck目录执行；output须是尚不存在的deck内目录
python3 "{SKILL_DIR}/scripts/template_library.py" apply --id "实际返回的id" --version v001 --deck-root . --output assets/templates/editorial
```

每个版本包含template.json、style.json、design-notes.md、assets。文件以内容hash核对并用原子方式发布；不覆盖旧版本或已存在的应用目录。list/show返回名称、id、版本与参考信息，不宣称脚本已看懂审美。

归档style.json的引用相对版本目录；apply复制后，style.json的图片引用重写为相对新deck-root的路径，可直接供extract_briefs.py --style-config使用。指定版本固定沿用；不指定版本读取最新版本，并在deck-state记下实际版本。内容hash不匹配时停止使用该版本，修复或另存新版本，不静默跳过图片。

## 下一套如何使用

用户说“用上次的暖白编辑风”时，查库匹配名称/标签，再查看描述和预览。只有多个候选无法确定时再做一次简短选择；找到唯一明确版本就直接载入，不重新泛搜风格。用户未指定模板时，不自动用最近收藏覆盖当前要求。

模板只约束设计：新素材、新品牌要求和当前比例优先；先做页型覆盖检查，缺数据页等只补局部规则或参考。重新建立可见文案清单，使用各自S/L/T角色防止旧客户名称、日期、数字及标题泄漏。品牌资产作为可选资料，按当前用户授权显式启用。复用后发生改进先留在当前deck；用户选择保存时创建新版本，原模板保持不变。

可用性证据分开：脚本测试证明文件独立、引用改写、版本与审批字段约束；真正的用户选择要来自实际对话。模板复用是否保持全篇视觉质量需新deck生成后按正常流程检查。
