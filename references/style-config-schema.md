# 风格配置：保存可复用的设计系统

用户选定方向后，把配色、字体角色、网格、页型和图表规则保存为 `assets/style-configs/{id}.json`。直接看参考图，再说明设计决策；视觉估计的 HEX 是近似设计值，不能宣称像素精确取色。已有品牌色、字体和模板约束优先。

这是配置协议，不是要求逐字照读的生成器提示词。数量由内容决定，不必凑够关键词、技法或场景数。仅保存已使用的规则。字体决策见 [字体方法](typography-method.md)。

本文件是单套deck内的设计配置。用户希望跨项目收藏时，按[模板库](template-library.md)在完成后确认，并将提炼后的配置与选定图片复制到独立本地库；不能只记旧项目路径。复用时重写到新deck路径，实际文案重新产生。

## 字段与权重

- `schemaVersion: 2`。基础字段为 `id/name/nameEn/keywords/tone/source/feeling/scenes/colors/colorLogic/techniques/styleWeight/styleRefImage/layoutSourceImage/layoutRefImages`，另有字体、网格、页型、间距和图表系统。
- 缺失的字段使用下文默认值；不自动改写原文件。同名配置更新先保留原文件，用户已授权修改时直接执行。
- `styleWeight` 控制 S 的跟随强度，范围 0–100，默认 90。`layoutWeight` 独立控制 L，范围 0–100，缺省 90；显式值总优先。
- 权重是提示词与是否附图的策略，不是 API 原生参数。S 在 styleWeight ≥ 70 时附图；L 在 layoutWeight ≥ 70 时附图；较低非零值将相应参考解析为文字指导，0 不跟随。用户明确要求保留或取消某张参考时优先。
- T 仅约束指定文字角色，不受这两项权重控制。只有本页使用的角色才附图。品牌资产 C 与内容保真也不受权重影响。
- `styleRefImage` 保存用户所选原图或代表单页裁片；模板整张联络表只保存在 `layoutSourceImage` 供分析，不传给生图。`layoutRefImages` 按所需页型裁原生分辨率单页，不为凑高清换掉选图。非模板来源可为 null。
- 风格板可以兼作风格参考（比如它同时画了贯穿全篇的角色）：`styleRefImage` 和 `styleBoardImage` 写同一张图就行，这张图只上传一次，锚定页、变奏页和亮点页都附它，稳定页仍附族锚定页。想让稳定页也附上它，拆分时加 `--style-ref-scope all`。
- `styleBoardImage`（可选）保存风格板：一张只含色卡、材质样本、典型元素与字形气质的非幻灯片图。存在且 styleWeight ≥ 70 时，`[节奏]` 标为变奏或亮点的页改附它（稳定 ID `[S-board]`），不再附族锚定页；稳定页不受影响。做法见[统一与变化](visual-director-method.md)。

## v2 配置示例

以下是示意配置，不是全局默认审美。完整可运行字体示例见 `assets/examples/typography-style.json`。

```json
{
  "schemaVersion": 2,
  "id": "clear-path",
  "name": "清晰路径",
  "nameEn": "Clear Path",
  "keywords": "开放结构、明确层级、温和对比",
  "tone": ["沉静", "清楚"],
  "source": "根据内容与品牌约束设计；颜色为设计选值",
  "feeling": "让复杂信息有清楚的阅读顺序",
  "scenes": ["方法讲解", "项目汇报"],
  "colors": {
    "primaryBackground": "#F5F3EE",
    "primaryBackgroundAlt": "#E7E6E1",
    "secondaryBackground": "#172D38",
    "titleColor": "#172D38",
    "textColor": "#34454D",
    "accentColor": "#B7472A",
    "decorColor": "#B9C8C8"
  },
  "colorLogic": "暖灰与深蓝灰形成阅读对比，赤陶色标记关键判断",
  "techniques": ["开放留白", "少量实线连接"],
  "styleWeight": 90,
  "layoutWeight": 80,
  "styleRefImage": null,
  "styleBoardImage": null,
  "layoutSourceImage": null,
  "layoutRefImages": null,
  "typography": {
    "roles": {
      "display": {
        "familyIntent": "与标题同源的方整展示字",
        "skeleton": "中宫开放，横向略宽，部首结构完整",
        "stroke": "粗笔画但保留内部空隙，末端只做微小斜切",
        "center": "重心稳定，不逐字跳动",
        "spacing": {"letterSpacingEm": 0.02, "lineHeight": 1.12},
        "effects": [],
        "sizeRatioToBody": 3.2,
        "usage": "封面或章节短标题"
      },
      "title": {
        "familyIntent": "开放方整的现代中文标题字",
        "skeleton": "方形字面，横竖比例均衡，中宫开放，部首关系完整",
        "stroke": "中粗且均匀，主笔收尾有很小的斜切，转折清楚",
        "center": "视觉重心稳定，汉字与数字基线协调",
        "spacing": {"letterSpacingEm": 0.015, "lineHeight": 1.18},
        "effects": [],
        "sizeRatioToBody": 2.1,
        "usage": "内容页主标题"
      },
      "body": {
        "familyIntent": "常规清晰无衬线正文",
        "skeleton": "开放字腔，标准部首结构",
        "stroke": "常规字重，不变形，不断笔",
        "center": "稳定基线",
        "spacing": {"letterSpacingEm": 0, "lineHeight": 1.5},
        "effects": [],
        "sizeRatioToBody": 1,
        "usage": "解释句、列表和证据文本"
      },
      "label": {
        "familyIntent": "与正文同源的清晰标签字",
        "stroke": "中等字重",
        "spacing": {"letterSpacingEm": 0.01, "lineHeight": 1.25},
        "effects": [],
        "sizeRatioToBody": 0.85,
        "usage": "图注、来源、图表标签"
      },
      "data": {
        "familyIntent": "与正文协调的清晰数字字形",
        "skeleton": "数字等高且便于列对齐，0/O、1/l 可区分",
        "stroke": "中等字重，小数点与负号清晰",
        "spacing": {"letterSpacingEm": 0, "lineHeight": 1.2},
        "effects": [],
        "sizeRatioToBody": 1.2,
        "usage": "关键指标、表格和图表数值"
      }
    },
    "references": {},
    "referencePolicy": {
      "mode": "as-needed",
      "defaultSpecimenRoles": ["title"],
      "scope": "glyph-only",
      "preserveExactText": true,
      "transferColor": false,
      "transferLayout": false,
      "transferSampleText": false
    }
  },
  "grid": {"columns": 12, "safeMarginPct": {"x": 5, "y": 6}, "gutterPct": 2},
  "spacing": {"unit": "percent-of-slide-height", "base": 1.5, "sectionGap": 4.5},
  "pageTypes": {
    "cover": {"textRoles": ["display", "label"], "intent": "一个核心承诺"},
    "content": {"textRoles": ["title", "body", "label"], "intent": "论点与证据"},
    "data": {"textRoles": ["title", "data", "label"], "intent": "支持比较与判断"}
  },
  "charts": {
    "axes": "按比较任务显示必要的刻度与单位",
    "seriesColors": ["#172D38", "#B7472A", "#748C92"],
    "emphasis": "只强调当前论点涉及的数据",
    "labels": "使用 label；数值使用 data；保留来源与口径"
  }
}
```

## 字段约束

`typography.roles` 仅使用 `display/title/body/label/data` 五个稳定角色键。每个对象描述一种字形，不把角色名称当作画面文字。`familyIntent` 是设计意图，不冒称已安装或取得许可的字体。可加 `fontFamily` 记录已核实的真实字体名；只有原生排版才有精确选字体、嵌入文件的含义，生图仍是视觉近似。

`skeleton/stroke/center` 描述字形；`spacing` 控制字距与行内关系；`effects` 默认空数组，只有 display/title 按需增加。`sizeRatioToBody` 是整套层级起点，必须在实际内容密度与最终尺寸下检查，不为比例强塞文字。正文、标签和数值不继承展示字装饰。保留输入简繁、标点、大小写、单位与符号。

`typography.references` 是已经生成且通过核字的角色→图片路径映射，例如 `{"title":"assets/typography/title.png"}`。没有图片时用 `{}`。计划中的路径可放可选 `referenceAssignments` 并标 `status: "planned"`，不能混进可执行引用。references 的键也只用五种角色。

`referencePolicy` 的保真与作用域字段是硬边界，不能为了样式自动转换文字、复制样例文本或整体布局。`mode: "off"` 禁用 T；`as-needed` 仅附本页明确指派的角色；`defaultSpecimenRoles` 是规划提示，不触发当前任务不需要的批量生成。缺失该对象时按示例边界与 `as-needed` 处理。

可选 `referenceAssignments` 用数组记录多图分工，例如 `{"id":"T-title","role":"typography","path":"assets/typography/title.png","appliesTo":"title","status":"verified"}`。执行引用由每页指派与实际文件生成；不要把数组顺序写死为“图 2”。`grid/spacing/pageTypes/charts` 按风格调整，比较或系列页可复用栅格，其他页按阅读任务变化。

## 保存检查

1. 配置与页面需求一致：有正文就有正文规则；数据页有数值、单位和来源规则。
2. 字号、行距和间距服务阅读，装饰不能覆盖字形正确与清晰度。
3. 权重独立；引用文件存在；模板整图没进入可执行引用；单张参考分工不矛盾。
4. 不强制浅底；文字与背景有足够辨识度；颜色是可执行设计值而非虚构测量结果。
