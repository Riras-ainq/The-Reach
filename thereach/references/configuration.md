# 配置、输出与访问验证

## 恢复与安装

基础调度只依赖 Python 3.9+ 标准库，无需 pip。平台能力复用用户已有工具：Agent Reach 当前实测 1.6.0、其环境的 yt-dlp 2026.8.19、OpenCLI 1.8.7、china-read（本机补充）。脚本不会安装、升级工具或复制登录状态。macOS Vision 用系统 Swift 与 Vision 框架，第一次编译可能较慢。

在项目根运行 `python3 scripts/install_skill.py --check` 检查重名，再运行 `python3 scripts/install_skill.py`。两处入口指向同一 Skill 目录。支持 `--codex-dir`、`--hermes-dir` 指定已有宿主配置的 skills 目录。安装不改 AGENTS.md、环境变量或 Hermes 配置。沙箱阻止写用户目录时，需由宿主授予这两个明确路径的写入权限。

Hermes 独立 profile 用 `--hermes-dir <profile>/skills` 安装；Docker 需用户已有挂载或在容器可访问位置部署整份 Skill，并在容器配置工具。Mac 的登录状态、文件路径和工具不会自动出现在容器。

配置示例是 [config.example.json](../config.example.json)；复制到项目根 `config.local.json` 后，把 `state_dir` 改为 `.thereach`。用全局参数 `--config /绝对路径/config.local.json` 或 `THEREACH_CONFIG` 读取。相对 state_dir 相对配置文件；无配置时相对项目根，不受调用者 cwd 影响。`THEREACH_STATE` 或 `--state-dir` 可显式选择共享状态目录。Codex/Hermes 必须共享这个目录，限速/验证码停止状态才能共享。

默认内容缓存 1 小时，普通失败缓存 2 分钟；验证码、登录与权限限制持久停止，人工确认后显式恢复；429 默认至少冷却 15 分钟，并尊重更长 Retry-After。BUSY 至少冷却 1 分钟。同平台调用间隔 10 秒、一个租约；不同进程通过 SQLite 互斥，租约超时自动释放。正常调用无重试循环，只有允许的不同路线有限降级。

`timeout` 为单次子进程/网络操作上限；`deadline` 是整个任务上限；`max_calls` 统计调度器的 HTTP 请求和子进程调用，**不能逐个计算 Agent Reach、yt-dlp 或连接器内部的请求**。子进程受外层超时、输出字节限制；直接 yt-dlp 路线已关闭自身重试。上游自己的浏览器管理和内部降级仍受上游策略控制。本工具不宣称精确计量供应商 Token 或费用，只限制外层付费/模型调用次数。

`max_bytes` 限制本进程接收/读取的输入数据，`max_media_bytes` 限制单个本地素材/直连图片；不控制上游转录工具内部临时下载量。`max_output_chars` 限制最终结构化输出；截断时明确标记，缓存保留原始取得文本。stderr/stdout 有界，错误日志不输出原始网页和命令凭据。

缓存目录 0700，SQLite 0600，输出默认私有文件。缓存可能保留小红书签名访问 URL 以供 enrich；导出移除内部 `_access_url`，并过滤常见密钥参数。不要提交缓存、真实配置、Cookie 或平台素材。输出仍可能含用户内容中的个人信息，应按用途分享。

## 命令

全局参数置于 `extract/search/media/probe/resume/doctor/status/login/check` **之前**。

```sh
python3 thereach/scripts/collect.py --deadline 90 --max-calls 8 extract '分享链接' --subtitles --comments --output outputs/item
python3 thereach/scripts/collect.py search '关键词' --platforms douyin xiaohongshu bilibili wechat youtube x --limit 2 --enrich 0 --output outputs/search
python3 thereach/scripts/collect.py --allow-paid --config config.local.json extract 'YouTube链接' --subtitles --transcribe --output outputs/asr
python3 thereach/scripts/collect.py resume xiaohongshu --human-confirmed
python3 thereach/scripts/collect.py status
python3 thereach/scripts/collect.py login bilibili
python3 thereach/scripts/collect.py login wechat '原文章链接'
python3 thereach/scripts/collect.py check bilibili subtitle '视频链接' --output outputs/check
```

`status` 不访问网络，返回六个平台的路线、当前 gate、各动作最近的实际执行证据（时间戳、后端、文本长度、范围及规范 URL）；未做过的动作保持空白。`check` 对一个 read/search/subtitle/comments 动作跳过内容缓存执行一次，但不跳过平台停止状态。`content_returned` 只表示该样本取得文本，不代表全文、全部评论或任意内容权限。无评论文本的返回不会记作内容已取得。正文读取与搜索命中分别保存证据。

`login` 只在现有 Agent Reach 专用浏览器打开官方页面，等待用户本人处理，不输入密码、不导出凭据、不自动 resume。B站使用官方网页，字幕及评论复用本机现有 OpenCLI 适配器；原生扫码工具曾显示成功但保存的 sessdata 为空。项目没有修改原工具或凭据文件。抖音和公众号的正文补充使用同一专用浏览器的已渲染 DOM；抖音只读取与请求视频 ID 对应的描述及当前已加载评论，不把章节摘要当字幕。

浏览器补充通过现有 Agent Reach 启动脚本定位其 Python，再复用其 Browser Bridge、查询锁与 OpenCLI 模块；未新增浏览器框架/服务。此部分依赖当前已安装版本的内部接口，升级上游后应重新 doctor/check，不能假定兼容。正文返回页与请求的规范 URL 不一致时拒收。

所有调用应使用同一个共享状态目录；不要另建状态目录绕过停止记录。

`--transcribe` 在本次字幕已取得时跳过 ASR；显式转录且无字幕时才调用。语音转录只有 YouTube 的 Agent Reach 路线开箱提供适配；其他平台配置获授权的 `transcribe_command`。本地已有音视频可用 `media <文件> --mode transcribe`，服务同样需授权及额度。默认不会下载本地模型。

输出基名生成 `.json` 和 `.md`，已有文件默认拒绝覆盖；只有显式 `--overwrite` 才覆盖。退出码 0 表示交付 complete/partial，2 表示失败；自动化应同时检查 `status` 和 `coverage`。搜索中失败平台分别列在 `platform_outcomes`，不会虚构零结果成功。

JSON v1 字段：`schema_version / ok / status / collected_at / items / platform_outcomes / metrics / error`。item 包含 `id / platform / url / title / author / published_at / text / text_scope / images / subtitles / transcript / comments / ocr / vision / coverage / provenance / warnings`。视频正文通常为 description。去重使用规范 URL，100 字符以上的 NFKC 归一化相同文本再跨平台去重，保留 duplicates；不做不可解释的语义合并。

默认输出摘要约数百至数千字符，含最多 3 条 160 字预览；不保证固定 Token 数，未实测宿主模型计费 Token。Markdown frontmatter 兼容 Obsidian 导入，不会自动修改 vault。

## 官方 API

- YouTube：`youtube_api: true` 并由进程环境提供 `THEREACH_YOUTUBE_KEY`，实现 videos/search/commentThreads，消耗 API 项目配额。密钥只在请求里使用，不写配置。字幕继续走已有字幕工具，API key 不是任意视频字幕下载权限。评论只取一页顶层评论，未展开全部回复。
- X：`x_api: true`、`THEREACH_X_BEARER`、`max_paid_calls > 0` 和命令 `--allow-paid` 同时满足后使用官方读取/最近搜索；回复通过 conversation_id 搜索，受时间窗口和账号权限限制。403/429 停止，不回退浏览器继续绕过。调用可计费，未配置则不消耗。
- API 成功返回的真实数据才算该次访问证据；doctor 中 key 存在、工具已安装或浏览器已登录不等于权限验证通过。本次没有凭据，以上官方 API 仅离线契约测试，未线上联调。

## 原生 AI / MCP 连接器

元宝、混元、豆包只在具备实际可调用且授权的客户端时配置。不接私有 App API，不把模型回答冒充原文。Skill 复用一个用户控制的命令适配器，命令可调用已有官方 SDK/MCP 客户端；本项目不安装或启动通用 MCP 服务。

```json
{
  "max_paid_calls": 2,
  "connectors": {
    "wechat": {
      "command": ["/绝对路径/python3", "/绝对路径/已审核的客户端.py"],
      "actions": ["read", "search"],
      "paid": true
    }
  }
}
```

连接器读取 stdin 单个 JSON：`schema_version, platform, action, target, limit, timeout_seconds, max_bytes, read_only`；输出单个 JSON：

```json
{"ok":true,"items":[{"url":"https://mp.weixin.qq.com/s/真实文章ID","title":"真实标题","text":"实际取得的原文","text_scope":"body"}]}
```

允许范围：read=`body/description`，search=`search_hit`，subtitle=`subtitle`，comments=`comment`。summary、纯 citations、无文本或无证据 URL 均不能通过 read 验权。失败返回 `{"ok":false,"error":{"code":"NEED_LOGIN","message":"..."}}`，错误码沿用 Agent Reach 或本项目文档。

用户选取自己能核对的真实原文片段（至少 12 字符），逐个动作执行：

```sh
python3 thereach/scripts/collect.py --config config.local.json --allow-paid probe wechat read '真实文章链接' --expected-url '同一文章链接' --expected-text '用户已经核对过的真实原文片段'
python3 thereach/scripts/collect.py --config config.local.json --allow-paid probe wechat search '关键词' --expected-url '预期命中的真实文章链接' --expected-text '预期真实搜索片段'
```

匹配结果 URL、文本及动作范围后记录 7 天的样本证据，配置和本地命令文件变化后需重验。read 通过不能代替 search/comments/subtitle。**probe 是样本验收，不是安全证明或完整访问权限证明**；不可信/会伪造数据的客户端仍不可采用。无通过记录时原生连接器不参与自动选路。真实平台权限受限时停止，不通过伪造 JSON 解除限制。

外部 `ocr_command`、`vision_command`、`transcribe_command` 同样为命令数组，读取 `action, path/target, timeout_seconds, read_only`，输出 `{"ok":true,"text":"实际识别结果","engine":"实际服务名"}`。外部识别均要求 `--allow-paid` 和付费预算，即使免费服务也按可能上传素材处理。Apple Vision 默认 OCR/分类在本机执行，无上传；分类仅标签和置信度，复杂图表/视频推理需外部视觉服务。

## 常见失败

| 错误 | 动作 |
|---|---|
| LOCAL_PERMISSION | 宿主沙箱或本地目录不可写；在获授权执行环境重试，不能解释成平台封禁 |
| NEED_SETUP / UNSUPPORTED | 看 doctor 和能力矩阵，配置已有工具/连接器；不反复尝试同一路线 |
| EMPTY_CONTENT / INVALID_RESPONSE | 页面壳、格式变更或工具返回无内容；有限降级，仍失败则报告缺口 |
| NEED_LOGIN / PLATFORM_BLOCKED / PERMISSION_DENIED | 停止该平台；用户在官方页面人工处理，确认后 resume |
| RATE_LIMITED / BUSY | 遵守冷却或等待其他调用结束，不切换后端 |
| NETWORK_ERROR / TIMEOUT | 一次有限降级，失败短期缓存；不假定必须登录 |
| BUDGET_EXCEEDED / OUTPUT_LIMIT | 保留已得结果，调整任务范围；不默认提高额度 |
