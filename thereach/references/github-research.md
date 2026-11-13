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
