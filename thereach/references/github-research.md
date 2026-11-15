# 依赖与致谢

TheReach 通过已有命令和浏览器接口复用成熟工具，没有将第三方项目核心代码复制进本仓库。

| 工具 | 来源 | 用途与许可说明 |
|---|---|---|
| Agent Reach | https://github.com/Panniantong/Agent-Reach | 统一平台 CLI、专用浏览器；上游 MIT |
| OpenCLI | https://github.com/jackwener/OpenCLI | Browser Bridge 与现有只读适配器；上游 Apache-2.0 |
| yt-dlp | https://github.com/yt-dlp/yt-dlp | 媒体元信息、字幕和有限评论；源码 Unlicense，分发及依赖另看上游 |
| Apple Vision | macOS 系统框架 | 本机 OCR 与图片标签分类 |

`china-read` 是原开发环境的本机补充适配器，不包含在本仓库，也不是安装 TheReach 后自动获得的命令。缺少时抖音搜索仅可尝试站外索引。

未安装或捆绑其他大规模抓取框架、本地大模型、账号池、代理轮换或验证码识别。依赖的许可证和平台使用条件应以相应上游当前文档为准；工具存在不代表获得任意内容访问权限。


## 本机语音转写补充（2026-11-15）

- [Apple SpeechAnalyzer](https://developer.apple.com/videos/play/wwdc2025/277/)：复用操作系统的已安装语音资源和时间信息；不引入额外模型下载或语音服务。
- [AliceDel66/BiliNote 音轨路线](https://github.com/AliceDel66/BiliNote/blob/main/lib/bilibili/playurl.ts)：参考字幕优先、最低码率独立音轨与来源区分的设计；未复制其核心实现。本项目复用已安装 OpenCLI 的 Page/WBI 模块。
- Whisper.cpp、MLX/Qwen3-ASR 和云端接口保留为候选，没有加入默认依赖。
