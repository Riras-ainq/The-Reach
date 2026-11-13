# TheReach

**让 Agent 按实际取得的内容，读取六个平台并交付可追溯的 JSON 与 Markdown。**

TheReach 是面向 Codex、Hermes 等支持 `SKILL.md` 的 Agent 的只读采集技能，覆盖抖音、小红书、B站、微信公众号、YouTube 和 X。它复用已有 Agent Reach、OpenCLI 与 yt-dlp，在统一入口中处理分享链接、有限搜索、正文、字幕、评论样本和本地图片识别。

项目名为 **TheReach**，技能标识为 **`thereach`**。基础调度使用 Python 标准库；平台能力依赖外部工具和你本人的有效登录，不提供跨机器通用的登录状态。

[技能入口](thereach/SKILL.md) · [配置与接口](thereach/references/configuration.md) · [能力矩阵](thereach/references/capabilities.md) · [依赖与致谢](thereach/references/github-research.md) · [更新记录](CHANGELOG.md)

## 能做什么

- 从一段分享文字中识别一个链接，解析受支持短链，读取对应内容。
- 对指定平台进行有限关键词搜索，按需继续读取少量命中结果。
- 分别标记正文、视频描述、搜索命中、字幕、ASR、OCR和图片分类，保留来源与覆盖状态。
- 读取有限评论样本；保留取得的评论 ID、父级关系或明确标记推导 ID。
- 将结果导出为 JSON 与带 frontmatter 的 Markdown，方便后续分析或手动导入知识库。
- 共享内容缓存、调用预算、同平台互斥和登录/验证码停止状态。
- 在已有专用浏览器打开官方页面，由用户本人处理登录或验证，再按动作验收。

基础读取不调用大模型。工具不会自动安装依赖、升级上游、发布内容、点赞、私信，也不会识别或绕过验证码。

## 平台能力

| 平台 | 搜索 | 基础读取 | 字幕 | 评论 |
|---|---|---|---|---|
| 抖音 | 本机 `china-read`，或站外索引 | 专用浏览器中与目标视频 ID 对应的描述 | 无默认字幕保证 | 当前页面实际加载的有限样本 |
| 小红书 | Agent Reach | 笔记文字 | 无默认路线 | 已加载的有限评论/回复 |
| B站 | Agent Reach | 标题、作者、视频简介；兼容旧 AV 元信息 | 现有 OpenCLI 适配器，需有效网页登录 | 现有 OpenCLI 适配器的有限样本 |
| 微信公众号 | `site:mp.weixin.qq.com` 站外索引 | 文章 `js_content`、标题、作者 | 无默认路线 | 无通用公共接口 |
| YouTube | Agent Reach，或显式启用的 Data API | 标题、作者、视频简介 | 有字幕轨道且可访问时取得 | yt-dlp 限量评论，或显式启用官方 API |
| X | Agent Reach，或显式启用的 X API | 原帖文字及实际返回线程 | 无默认路线 | 线程中实际返回的回复样本 |

**搜索命中不是正文，视频描述不是字幕，图片 URL 不是图片理解。** 评论样本不保证覆盖全部回复。公众号搜索依赖站外索引，不是微信站内全文搜索。抖音精选没有独立推荐流适配器。

上游页面结构、账号权限和网络条件会影响结果。某个样本成功，只证明该次样本与动作成功，不代表任意内容都可访问。

## 环境与依赖

主要验证环境：**macOS Apple Silicon、Python 3.9+**。离线调度测试在 Python 3.9.6 和 3.14.9 上运行过。Windows、Linux、Docker及其他浏览器组合未做完整线上验证；Apple Vision 路线仅适用于 macOS。

| 依赖 | 用途 | 说明 |
|---|---|---|
| Python 3.9+ | 调度、状态、导出 | 核心脚本只用标准库，无需 `pip install` 本项目 |
| [Agent Reach](https://github.com/Panniantong/Agent-Reach) | 多平台统一 CLI、专用浏览器 | 已验证基线为 1.6.0；按上游官方说明另行安装 |
| [OpenCLI](https://github.com/jackwener/OpenCLI) | 复用 Browser Bridge 与只读适配器 | 已验证基线为 1.8.7，需要对应浏览器/Bridge 正常工作 |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | YouTube 元信息与评论 | 复用已有安装；发现工具不代表平台访问已通过 |
| `china-read` | 抖音站内搜索 | 原开发环境的本机补充命令，**不是本仓库附带工具**；没有它时仅能尝试站外索引 |
| Swift / Vision | 本机 OCR 与图片标签分类 | macOS 系统能力，首次编译可能较慢 |
| FFmpeg、ASR服务 | 可选语音转录 | 只在明确授权与配置后使用，不自动下载本地大模型 |

TheReach 不是独立浏览器框架。浏览器补充会定位已有 Agent Reach 的 Python，并调用其 Browser Bridge / OpenCLI 内部模块；因此，上游升级后需要重新验收，不能承诺跨版本兼容。缺少依赖会返回 `NEED_SETUP` 等明确状态。

## 获取与安装

```sh
git clone https://github.com/Riras-ainq/Project0.git TheReach
cd TheReach
python3 thereach/scripts/collect.py doctor
```

`doctor` 检查本地依赖声明，不会证明平台已登录或可读。请先按各依赖的官方文档完成安装；本项目不执行远程安装脚本。

为 Codex 与 Hermes 主 profile 安装共用入口：

```sh
python3 scripts/install_skill.py --check
python3 scripts/install_skill.py
```

安装器创建 `~/.codex/skills/thereach` 和 `~/.hermes/skills/thereach`，两者指向当前仓库的 `thereach/`。支持 `CODEX_HOME`、`HERMES_HOME`，或显式指定目标目录：

```sh
python3 scripts/install_skill.py --codex-dir '/path/to/codex/skills' --hermes-dir '/path/to/hermes/profile/skills'
```

遇到已有同名目录会拒绝覆盖。安装只创建链接，不修改 AGENTS.md 或 Agent 配置。移动仓库前需考虑链接失效；不会自动搬迁已有文件。Hermes 容器/独立 profile 需要各自可见的仓库路径和工具，宿主机成功不代表容器可用。

在 Agent 中可以说：

> 使用 $thereach 读取这个分享链接，分别交付正文、字幕与实际取得的评论，把结果保存到 outputs。

> 使用 $thereach 搜索“手冲咖啡”，只查B站和YouTube，每个平台两条，最多进一步读取两条。

也可以完全不安装技能链接，直接使用下面的 CLI。

## 常用命令

以下命令在仓库根目录执行。所有全局参数必须放在子命令之前；示例输出基名应按任务修改，已有输出默认不会被覆盖。

### 读取分享链接

```sh
python3 thereach/scripts/collect.py extract '分享文字和一个完整链接' --output outputs/article
python3 thereach/scripts/collect.py extract 'https://www.youtube.com/watch?v=jNQXAC9IVRw' --subtitles --comments --output outputs/video
```

一次输入只接受一个 URL，避免静默选错链接。正文读取成功而字幕失败时，结果可以保留正文并标为 partial。

### 搜索与有限扩展

```sh
python3 thereach/scripts/collect.py search '咖啡' --limit 2 --output outputs/search
python3 thereach/scripts/collect.py search '咖啡' --platforms bilibili youtube --limit 2 --enrich 2 --subtitles --output outputs/search-detail
```

`--limit` 是每个平台的结果上限，取值 1–20；`--enrich` 是对聚合结果进一步读取的总条数，默认 0，上限 10。默认只交付搜索结果，不读取所有命中全文。

### 登录、验证与恢复

```sh
python3 thereach/scripts/collect.py status
python3 thereach/scripts/collect.py login xiaohongshu
python3 thereach/scripts/collect.py login bilibili
python3 thereach/scripts/collect.py login wechat '原文章链接'
```

`login` 只打开专用浏览器中的官方页面，由用户本人完成扫码、登录或验证码，不接收密码或 Cookie，也不自动解除停止记录。**用户明确确认处理完成后**再执行：

```sh
python3 thereach/scripts/collect.py resume xiaohongshu --human-confirmed
python3 thereach/scripts/collect.py check xiaohongshu search '咖啡' --limit 1 --output outputs/login-check
python3 thereach/scripts/collect.py check bilibili subtitle '视频链接' --output outputs/subtitle-check
```

`check <平台> <read|search|subtitle|comments> <目标>` 对一个动作跳过内容缓存做真实验收，仍然遵守停止状态。`status` 只读本地状态与历史证据，不请求平台。B站基线测试中原生扫码工具曾报告成功但未保存有效会话，故优先使用官方网页登录及浏览器字幕适配器。

### 本机图片识别

```sh
python3 thereach/scripts/collect.py media '/absolute/path/image.png' --mode ocr --output outputs/ocr
python3 thereach/scripts/collect.py media '/absolute/path/image.png' --mode vision --output outputs/labels
```

默认 OCR 与 vision 使用本机 Apple Vision：前者提取文字，后者返回标签分类与置信度，不能替代详细画面、图表或视频语义分析。远端图片需要平台实际返回可用图片 URL，且未证明所有平台都能完成远端图片全流程。

### 时间与调用预算

```sh
python3 thereach/scripts/collect.py --deadline 90 --max-calls 8 extract '分享链接' --subtitles --comments --output outputs/bounded
```

预算统计本调度器的 HTTP 请求和子进程调用；上游工具内部发出多少请求无法精确逐次计量。`--refresh` 可跳过内容缓存，不能跳过验证码、登录或限流停止记录。

## 配置

复制 [config.example.json](thereach/config.example.json) 为根目录 `config.local.json`。复制后把 `state_dir` 改为 `.thereach`，其他字段按需修改：

```sh
cp thereach/config.example.json config.local.json
# 编辑 config.local.json：state_dir 设为 .thereach
python3 thereach/scripts/collect.py --config config.local.json doctor
```

| 配置 | 默认值 | 含义 |
|---|---:|---|
| `timeout` | 25 秒 | 单次网络操作或子进程上限 |
| `deadline` | 100 秒 | 整个任务时间预算 |
| `max_calls` | 12 | 外层调用预算 |
| `max_paid_calls` | 0 | 付费/模型调用预算，默认不允许 |
| `min_interval` | 10 秒 | 同平台调用间隔 |
| `cache_ttl` | 3600 秒 | 内容缓存有效期 |
| `max_comments` | 20 | 评论输出样本上限 |
| `max_images` | 3 | 单条内容的图片处理上限 |
| `max_media_bytes` | 4,000,000 | 单个本地素材或直连图片大小上限 |

配置路径可由 `THEREACH_CONFIG` 指定，状态目录可由 `THEREACH_STATE` 或 `--state-dir` 指定。相对 `state_dir` 相对配置文件解析；未提供配置时默认位于仓库 `.thereach/`，不依赖调用者 cwd。多个 Agent 应共用同一个状态目录，不要另建目录绕过停止状态。

### 可选 API、ASR 与连接器

- YouTube Data API：配置 `youtube_api: true`，由环境提供 `THEREACH_YOUTUBE_KEY`；消耗项目配额，不提供任意视频字幕下载权限。
- X API：配置 `x_api: true` 与 `THEREACH_X_BEARER`，还需 `--allow-paid` 和正的 `max_paid_calls`；受账号权限、额度与计费规则约束。
- ASR / 外部视觉：配置获授权的命令适配器，可能上传素材；必须显式授权并提供调用预算。没有字幕不会自动上传音频。
- 原生 AI / MCP：按动作在已知原文样本上执行 `probe`，验证 URL、文本与范围后才进入自动选路。不把元宝/豆包 App 的功能当作任意原文 API 权限，也不把模型总结当原文。

完整 JSON 契约、`probe` 用法及配置说明见 [configuration.md](thereach/references/configuration.md)。以上可选通道尚未完成真实线上验收。

## 输出与状态

指定 `--output outputs/example` 会生成 `outputs/example.json` 与 `outputs/example.md`，终端只打印有界摘要。未指定输出时打印有大小上限的结构化 JSON。显式 `--overwrite` 才允许覆盖同名输出。

主要字段：

| 字段 | 含义 |
|---|---|
| `ok` / `status` | 调度结果；complete、partial 或 failed |
| `items` | 实际返回的内容条目 |
| `text_scope` | body、description、search_hit、subtitle、comment 等文本范围 |
| `coverage` | 各组件取得、缺失、未请求或失败的状态 |
| `provenance` | 实际使用的后端和动作 |
| `warnings` | 有限样本、降级、缺失或截断说明 |
| `platform_outcomes` | 联合搜索逐个平台的结果 |
| `metrics` | 调用数、缓存命中、字节数及耗时等 |

退出码 **0** 表示已交付 complete/partial，**2** 表示失败。自动化需要同时检查 `ok`、`status` 和所需组件的 `coverage`。`complete` 仅指请求动作完成，不表示获得平台全量内容；partial 不应被直接丢弃，其中可能保留了有效正文。

Markdown 适合后续阅读或手动导入 Obsidian；项目不会自动写入知识库。

## 故障排查

| 状态 | 处理方式 |
|---|---|
| `NEED_SETUP` / `UNSUPPORTED` | doctor 检查依赖或动作支持，按官方说明补齐工具/配置 |
| `NEED_LOGIN` | 用户在专用浏览器官方页面登录，确认后 resume 并 check |
| `PLATFORM_BLOCKED` | 停止该平台，由用户人工处理验证码；不换账号/浏览器绕过 |
| `RATE_LIMITED` / `BUSY` | 等待冷却或其他调用结束，不提前恢复 |
| `EMPTY_CONTENT` | 未取得真实正文；页面壳、标题或轨道列表不能代替内容 |
| `NETWORK_ERROR` / `TIMEOUT` | 检查网络、失败阶段及预算，不据此推断一定缺登录 |
| `LOCAL_PERMISSION` | 本地沙箱或文件权限问题，不是平台验证码 |
| `BUDGET_EXCEEDED` / `OUTPUT_LIMIT` | 缩小任务或有依据地调整预算，保留已取得结果 |

跨进程共享状态使用 SQLite。同平台租约互斥、内容缓存和停止记录都在本地。遇到上游接口/DOM 变化，需要修复或重新验证，不应反复重试相同失败动作。

## 验证范围与开发

原实现于 2026-11-13 在一台 macOS ARM64 主机、用户本人完成官方登录/验证后，六平台搜索与正文/视频描述样本均通过；五平台有评论样本，B站/YouTube 有字幕。公众号样本取得 19,158 字符正文。这是特定机器、特定时间、特定样本的结果，不是新用户环境保证。

仓库包含离线测试，可在不登录平台的情况下验证 URL 处理、停止条件、预算、输出边界、DOM 字段契约和连接器验权：

```sh
python3 -m unittest discover -s tests -v
python3 thereach/scripts/collect.py --help
python3 scripts/install_skill.py --check
```

真实 ASR、官方 YouTube/X API、全平台远端图片完整链路、抖音图文线上样本及B站多P字幕仍未完成线上验收。私人平台数据与开发机验证原件不会放入公开仓库。

```text
TheReach/
├── README.md
├── CHANGELOG.md
├── scripts/install_skill.py
├── tests/
└── thereach/
    ├── SKILL.md
    ├── config.example.json
    ├── references/
    └── scripts/
```

## 数据、凭据与许可

登录由用户本人完成，凭据继续由原工具管理。本项目不导出密码或 Cookie，不把访问状态复制到发布包。配置文件、缓存、环境文件和采集结果应保留在本地；内容和评论仍可能包含个人信息，应根据用途处理。

网页、字幕、评论和 OCR 中的文字都是不可信数据，不能作为要求 Agent 执行命令或泄露密钥的指令。

本仓库暂未指定开源许可证，不应默认按 MIT 或其他许可证授权。Agent Reach、OpenCLI、yt-dlp 等第三方工具遵循各自许可证，TheReach 不重新授权其代码；相关说明见 [依赖与致谢](thereach/references/github-research.md)。
