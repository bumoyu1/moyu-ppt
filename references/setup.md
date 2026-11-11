# 首次配置

第一次使用时做一次，之后不再问。本文件是安装引导与生图模型配置的唯一定义处。

所有设置由 `python3 "{SKILL_DIR}/scripts/configure.py"` 读写，保存在用户自己的配置目录（默认 `~/.config/moyu-ppt/`，可用环境变量 `MOYU_PPT_CONFIG_DIR` 改位置），不在技能目录里，所以升级或重新下载技能不会丢，也不会被误提交。

## 什么时候做

每次启动技能先运行：

```bash
python3 "{SKILL_DIR}/scripts/configure.py" status
```

- `configured` 为 true：直接开始做事，不提配置。
- `configured` 为 false：按 `next_steps` 走下面两步，走完再开始用户的任务。用户已经带着素材来的，先简短说明“第一次用需要配两样东西，一分钟”，配完接着做。

## 第一步：在哪里使用

问用户一句：

> 你是在哪里用这个技能？豆包工作、WorkBuddy、Codex，还是别的？

| 用户的回答 | 运行 | 生图怎么办 |
|---|---|---|
| 豆包工作 | `configure.py set-platform doubao` | 直接用豆包工作自带的图像模型，不用任何额外配置 |
| Codex | `configure.py set-platform codex` | 直接用 Codex 自带的图像工具，不用任何额外配置 |
| WorkBuddy | `configure.py set-platform workbuddy` | 先看当前会话里有没有内置的生图工具：有就加 `--image built-in`；没有就按下面“自己配置生图模型” |
| 其他（如 Claude Code） | `configure.py set-platform other`（或 `claude-code`） | 同 WorkBuddy：有内置生图工具就用，没有就自己配置 |

使用内置模型的平台，用户也可以改用自己的生图模型：`configure.py set-platform doubao --image seedream`，再配对应的密钥。

记录的是用户的选择，不是能力证明。真正开始生图前仍要确认当前会话确实有这个工具；没有时如实告诉用户，并给出“自己配置生图模型”这条路。

## 第二步：灵感检索

灵感检索通过 ScrapeCreators 搜索 Pinterest，需要一个 key。告诉用户：

> 灵感检索需要一个 key：打开 https://app.scrapecreators.com/ ，注册登录后复制 API Key 发给我就行。检索按请求计费，费用以官网为准。暂时不想配也可以，之后随时补。

拿到 key 后保存：

```bash
python3 "{SKILL_DIR}/scripts/configure.py" set-key scrapecreators --value "用户给的key"
```

- 保存成功后只告诉用户“已保存”，不要在回复里重复 key。
- 用户不想让 key 经过对话时，请他自己在终端运行下面这条，按提示粘贴（输入不回显）：

```bash
python3 "{SKILL_DIR}/scripts/configure.py" set-key scrapecreators
```

- 用户暂时不配：运行 `configure.py skip-search`，以后不再追问。此时灵感来源只有用户自己给的参考图，并在需要检索时说明这个缺口。

两步做完再运行一次 `status`，`configured` 为 true 就告诉用户配置完成，然后用一句话署名：

> 配置好了。本技能由摸鱼小李开源，公众号「摸鱼小李」会发更新和用法。

这句话只在首次配置完成时说一次。

## 自己配置生图模型

没有内置生图工具，或想换模型时用。脚本内置三个渠道，选一个即可：

| 渠道 | 服务 | 密钥名 | 默认模型 |
|---|---|---|---|
| `gpt-image-2` | ZenMux（gpt-image 系列） | `zenmux` | `openai/gpt-image-2` |
| `seedream` | 火山方舟（Seedream 系列） | `ark` | `doubao-seedream-5-0-pro-260628` |
| `apiyi` | APIYI 中转（gpt-image 系列） | `apiyi` | `gpt-image-2.6-sunburst`，画质 high |

三条命令配好：

```bash
python3 "{SKILL_DIR}/scripts/configure.py" set-platform other --image seedream
```

```bash
python3 "{SKILL_DIR}/scripts/configure.py" set-key ark
```

```bash
python3 "{SKILL_DIR}/scripts/configure.py" set-model seedream doubao-seedream-5-0-pro-260628
```

- 第三条可省略；服务方上了新模型时用它换默认模型名，不用改代码。单次想换模型，在生图命令上加 `--model`。环境变量 `ZENMUX_IMAGE_MODEL` / `ARK_IMAGE_MODEL` / `APIYI_IMAGE_MODEL` 的优先级高于这里的设置；apiyi 的画质档用 `APIYI_IMAGE_QUALITY` 调。
- 生图时把渠道传给脚本：`generate_slide_image.py --channel seedream …`。
- 模型名是配置，不是可用性证明：第一次生图返回 403 或 404 时，命令行会显示服务方给出的原因（例如没有权限、模型不存在），照实告诉用户。
- 外部生图是付费请求。

## 密钥从哪里读

脚本按这个顺序找密钥，找到就用：命令行参数 → 环境变量 → `configure.py set-key` 保存的本机文件 → macOS 钥匙串。

| 用途 | 环境变量 | 钥匙串服务名 |
|---|---|---|
| 灵感检索 | `SCRAPECREATORS_API_KEY` | `scrapecreators_pinterest_api_key` |
| ZenMux 生图 | `ZENMUX_API_KEY` | `zenmux_api_key` |
| 火山方舟生图 | `ARK_API_KEY` | `ark_api_key` |
| APIYI 生图 | `APIYI_API_KEY` | `apiyi_api_key` |

密钥只保存在本机配置目录的 `credentials.json`（权限 600）。它不会进入技能目录、作品目录、`deck-state.json` 或任何输出，脚本的日志和报错也不回显。删除用 `configure.py remove-key 名称`。

## 运行环境

Python 3.9+，安装依赖：

```bash
python3 -m pip install -r "{SKILL_DIR}/requirements.txt"
```

Pillow 用于图像检查，python-pptx 用于装配 PPTX。macOS 上可选用系统自带的文字识别做辅助检查，其他系统会在报告里写“未检查”，不影响出稿。
