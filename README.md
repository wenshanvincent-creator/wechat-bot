# 鲸鱼电台 · WeChat Bot

普通微信群和好友的 AI 聊天机器人：真实 Linux 微信客户端、托管模型 API、中文控制台、独立记忆和受控工具。

## 功能

- OpenAI Chat Completions 兼容 API，可切换模型与人设。单微信账号下每个会话只激活一个人设；回复可拆成多条消息。
- 群名片优先；消息包含用于区分账号的身份标识、昵称、聊天名、时间和正文，移除传输前缀与原生回执。聊天的读取、发送、主动私聊分别授权。
- 多个聊天可绑定同一会话；短期上下文按会话共享，长期记忆按人设 × 会话存 SQLite。上下文默认到 50% 自动压缩。
- LangMem 长期事实、独立原文档案、关键词检索/原文翻页、每日记忆和相处摘要。当前来源权限参与档案检索。
- 图片与表情按内容哈希识别一次，后续使用短描述；支持模型审核、命名、收录和发送表情。
- 课表、课前提醒、班务事项与提醒；指定管理联系人可通过工具维护。跨聊天发言只面向已授权聊天。
- DeepSeek Harness 工具容器无外网、无微信资料、无真实 API key；系统命令经控制台逐条批准。
- 好友申请和添加好友工具；建立好友关系不会自动授予消息读取或回复权限。
- 文字朋友圈草稿与发布记录；自主发布默认关闭。客户端发布失败会记录失败。

多账号和群内机器人互聊目前尚未实现，开关保持关闭。所有模型均使用托管 API，不运行本地模型，不处理视频。

## 仓库与运行环境

```text
runtime/       Python 运行器、控制台、原生读取与 UI 自动化
tests/         离线测试，使用合成聊天和临时数据库
deploy/        Ubuntu Compose、HTTPS、宿主审批执行器
tools/         tokenizer 下载与镜像核对
third_party/   人设许可与记忆设计来源
config.json    初始关闭、无微信账号和聊天绑定的模板
personality.md 可编辑人设
```

此仓库发布应用代码，不包含微信登录、聊天记录、记忆库、密钥、个人服务器配置、社区表情图片或 Docker 镜像包。

生产运行环境为 Ubuntu 24.04 / amd64，建议 2 核 4GB。它依赖固定版本的微信桌面镜像
`attention-wechat-desktop:4.1.13.26-trial-atspi`；该定制镜像及其完整构建上下文未在此发布。
**仅克隆本仓库不能直接启动微信。** Dockerfile.bot 和 Dockerfile.harness 构建后两层镜像，前提是已具备匹配的基础镜像。

## 开发与离线检查

Python 3.12+：

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
```

离线测试不需要微信账号、API key、模型推理或社区图片。真实收发验收脚本不在 pytest 套件内；
`runtime/acceptance_live.py --channel CHANNEL_ID --send` 会真实发送消息，请只对明确授权的测试聊天执行。

## 部署入口

见 [部署说明](docs/deployment.md)。已有匹配基础镜像时，先下载官方 tokenizer 数据，再构建后两层镜像：

```bash
python tools/download-tokenizer.py
docker build -f Dockerfile.bot -t wechat-deepseek-memory:local .
docker build -f Dockerfile.harness -t wechat-deepseek-harness:local .
```

`images/manifest.json` 是一批已验证镜像的身份清单，用于识别匹配版本，不是镜像下载地址。
重新构建的镜像 ID 可能不同，需要自行确认客户端版本和 UI 模板是否匹配。

## 来源

- 人设改编自 [dsh-whale-musume-persona](https://github.com/Kaalia0912/dsh-whale-musume-persona)，保留性格，去掉动作描写并减少卖萌；MIT 许可见 third_party/whale-persona/。
- 记忆设计参考 [dsh-world](https://github.com/chenkezhen480/dsh-world)，在 Python 运行器内独立实现；说明见 third_party/dsh-world/。
- tokenizer 来源与固定哈希见 assets/deepseek-v4-tokenizer/SOURCE.md。下载工具只读取 JSON，不执行下载的 Python 代码。

微信数据库结构和 UI 自动化依赖固定客户端版本；升级客户端后需要重新验证读取、窗口绑定和发送回执。
