# 部署

准备 Ubuntu 24.04、amd64、Docker Engine/Compose，以及 README 指定的三个匹配镜像。
应用的 Python 文件通过只读挂载进入容器；不用把运行资料复制到代码仓库。

## 首次运行

1. 将仓库放在服务器目录，例如 /opt/wechat-agent。
2. 按 README 准备镜像和 tokenizer。使用已有冻结镜像包时，先核对其归档 SHA256 和 images/manifest.json。
3. 复制 deploy/.env.example 为 deploy/.env，私下填 DEEPSEEK_API_KEY、WECHAT_DESKTOP_PASSWORD、CONSOLE_PASSWORD，设 chmod 600。保留 LF 行尾。
4. 运行 bash deploy/first-start.sh，初始化数据卷并启动四个服务。
5. 控制台和微信桌面分别监听服务器的 127.0.0.1:13332、127.0.0.1:13331。先通过 SSH 本地转发访问，密码分别使用上一步设置的值；桌面用户名 bot。
6. 微信扫码登录后，在控制台绑定当前账号、刷新目录、逐个添加聊天、指定会话与人设，再绑定并核对聊天窗口。确认授权后开启自动聊天。

```bash
ssh -L 13332:127.0.0.1:13332 -L 13331:127.0.0.1:13331 USER@SERVER
```

首次使用时不绑定任何账号或聊天，自动发送关闭。多个聊天只有选择同一会话才共享上下文。
新好友的读取、发送、主动私聊权限默认不因加好友而开启。

## 公网 IP 访问

deploy/setup-ip-https.sh PUBLIC_IPV4 CERTIFICATE_CONTACT_EMAIL 配置 nginx、IP 证书及续期服务。
该脚本需要 root，会安装软件、修改 nginx 和 systemd，并调用 ACME 服务接受其服务条款。
请阅读脚本后运行，预先配置所需的 80/443/9443 防火墙规则。
控制台访问 https://PUBLIC_IPV4/，微信桌面访问 https://PUBLIC_IPV4:9443/；容器端口保持回环监听。

## 工具与维护

宿主执行器可用 bash deploy/install-admin-executor.sh 安装；它仅执行控制台批准的具体命令。
Harness 保持 network_mode: none、非 root、只读根，不挂载密钥、微信资料或 Docker socket。

```bash
cd deploy
docker compose --env-file .env -f compose.server.yaml ps
docker compose --env-file .env -f compose.server.yaml logs --tail 100 worker
docker compose --env-file .env -f compose.server.yaml stop worker harness
```

代码更新后只重启受影响服务。不要把 deploy/.env、微信 profile、SQLite 状态、媒体缓存、日志或私有导出包提交到 Git。
更新 UI 自动化代码或微信版本时，重新核对窗口绑定、实际聊天标题与原生发送回执。
文字朋友圈是否可发布取决于客户端状态；提交不等于已在信息流发布，失败回执应保持失败。
