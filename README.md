# tg2qqqun

一个将指定 **Telegram 公共频道**的新消息转发到 **QQ 群** 的小工具，基于：
- Telegram：Telethon
- QQ：NapCat OneBot v11 HTTP
- 部署：Docker Compose

本项目针对部分 NapCat 环境下 **直接发送 `image` 段可能报** `rich media transfer failed` 的问题，采用更稳定的发送策略：
- 使用 `send_group_forward_msg` 发送**合并转发**
- 节点顺序固定为：**图片在前、文字在后**（点开后“上图下文”）

---

## 功能特性

- 监听指定 Telegram 公开频道/群（账号需已加入；支持多个来源）
- 转发到 QQ 群（NapCat OneBot v11 HTTP；支持一次转发到多个群 `group_ids`）
- 文本清洗（可配置 rules）：
  - 从 “📤 资源链接” 开始到结尾的尾巴段落删除
  - 多空行收敛
  - 追加自定义模板，并自动提取 `{title}`（从第一行标题提取）
- 图片落盘后由 NapCat 通过 `file://` 读取发送
- **持久化去重**：`chat_id + msg_id`，避免容器重启重复转发（SQLite）
- **媒体保留策略**：定期删除过期图片，防止目录无限增长
- 可选过滤器：关键词/正则/白名单（命中则丢弃不转发）

---

## 运行前提

- Linux 服务器（建议）
- 已安装 Docker / Docker Compose
- Telegram 账号 1 个（用于登录并加入要监听的频道）
- Telegram `api_id` / `api_hash`
- QQ 侧需要 **NapCat（OneBot v11 HTTP）**：
  - tg2qqqun 会调用 OneBot HTTP 发送消息
  - 如果你还没安装 NapCat，请先看下文《安装/运行 NapCat（可选示例）》

---

## 选择你的安装方式（按你的情况看对应章节）

- **A. 已经有 NapCat**（你已经在服务器上运行了 NapCat，并开启了 OneBot v11 HTTP）：
  - 只需要部署本项目转发器：看《部署方式 A：已安装 NapCat，仅部署 tg2qqqun》
- **B. 还没有 NapCat**（希望一台机器上一次性部署 NapCat + tg2qqqun）：
  - 直接使用本仓库提供的组合 compose：看《部署方式 B：未安装 NapCat，一键部署 NapCat + tg2qqqun》

---

## 部署方式 A：已安装 NapCat，仅部署 tg2qqqun（部署到宿主机 /home/tg2qqqun）

> 适用：你已经有 NapCat；本章节只部署 tg2qqqun。本项目会通过 `qq.onebot_base_url` + `qq.token` 调用 NapCat 的 OneBot HTTP。

### 1) 放置项目文件

将本仓库文件放到宿主机目录：

- `/home/tg2qqqun`

目录结构包含：

- `docker-compose.yml`
- `config/config.yaml`
- `app/`
- `session/`（会生成 Telegram session 与去重数据库）

### 2) 创建媒体目录

创建图片落盘目录：

- `/home/tg2qqqun/data/tg_media`

### 3) 配置 NapCat 挂载（关键）

> 目标：让 NapCat 容器能读取到 tg2qqqun 下载的图片，用于 `file://` 发图。

tg2qqqun 会把 Telegram 图片下载到宿主机：
- `/home/tg2qqqun/data/tg_media`

请将宿主机目录挂载到 NapCat 容器内：
- `/AstrBot/data/tg_media`（建议 `:ro`）

> tg2qqqun 发送图片时会引用：`file:///AstrBot/data/tg_media/<filename>`，路径必须一致。

### 4) 在 NapCat WebUI 启用 OneBot v11 HTTP Server

（如果你已经启用过并确认可用，可跳过）

- Host：`0.0.0.0`
- Port：`3000`
- 消息格式：`Array`
- Token：设置后记下来

### 5) 修改配置 `config/config.yaml`

至少需要修改：
- `telegram.api_id`
- `telegram.api_hash`
- `telegram.sources`（要监听的来源列表；可配置多个）
- `qq.onebot_base_url`（NapCat OneBot HTTP 地址）
- `qq.token`（NapCat HTTP token）
- `qq.group_ids`（目标 QQ 群号列表；填 1 个就是单群，填多个就是多群）

### 6) 首次登录 Telegram（生成 session，推荐方式 A）

在宿主机 `/home/tg2qqqun` 执行：

- `docker compose run --rm tg2qq python login.py`

成功后会生成：
- `/home/tg2qqqun/session/telegram.session`

### 7) 启动转发器

- `docker compose up -d --build`

查看日志：
- `docker logs -f tg2qq`

停止：
- `docker compose down`

---

## 部署方式 B：未安装 NapCat，一键部署 NapCat + tg2qqqun

> 适用：你还没有 NapCat，想要直接用本仓库提供的 `napcat/docker-compose.yml` 同时拉起 NapCat + tg2qqqun。

### 1) 配置 tg2qqqun

依然需要先改本项目配置：`/home/tg2qqqun/config/config.yaml`（或使用 `config/config.example.yaml` 复制一份）：
- `telegram.api_id` / `telegram.api_hash`
- `telegram.sources`
- `qq.token`（需要与你在 NapCat WebUI 中设置的 Token 一致）
- `qq.group_ids`

### 2) 启动组合 compose

在服务器进入目录：`/home/tg2qqqun/napcat`，使用其中的 `docker-compose.yml` 启动。

> 注意：NapCat 官方要求设置 `NAPCAT_UID/NAPCAT_GID` 用于挂载目录权限。

推荐做法：在 `napcat/` 目录创建 `.env`（compose 会自动读取）：

- `NAPCAT_UID=<你的Linux用户uid>`
- `NAPCAT_GID=<你的Linux用户gid>`

示例命令（在宿主机 Linux 上执行）：
- `cd /home/tg2qqqun/napcat`
- `echo "NAPCAT_UID=$(id -u)" > .env`
- `echo "NAPCAT_GID=$(id -g)" >> .env`
- `docker compose up -d --build`

查看两边日志：
- `docker logs -f napcat`
- `docker logs -f tg2qq`

### 3) 打开 NapCat WebUI 并启用 OneBot HTTP

WebUI：`http://<宿主机IP>:6099/webui`

按《安装/运行 NapCat（WebUI 配置说明）》中的步骤创建 **HTTP服务器**：
- Host：`0.0.0.0`
- Port：`3000`
- 消息格式：`Array`
- Token：与 `config/config.yaml -> qq.token` 保持一致

---

## 配置说明（config/config.yaml）

### telegram
- `api_id` / `api_hash`：Telegram 开发者凭据（获取方式见下文）
- `session_path`：容器内会话文件路径，默认 `/session/telegram.session`
- `sources`：监听来源列表（支持多个）。推荐写 `"@username"`；也支持 `https://t.me/xxx`。

### qq
- `onebot_base_url`：NapCat OneBot HTTP 地址（默认 `http://127.0.0.1:3000`）
- `token`：HTTP token（请求头 `Authorization: Bearer <token>`）
- `group_ids`：目标 QQ 群号列表（非空）。每条消息会“尽量发到所有群”，单群失败不影响其它群。

### storage
- `host_media_dir_in_container`：tg2qq 容器内写入目录（保存 TG 图片），默认 `/host_tg_media`
- `napcat_media_dir_in_container`：NapCat 容器内读取目录（用于 `file://`），默认 `/AstrBot/data/tg_media`

### media_retention（媒体自动清理）
- `enabled`：是否启用
- `dir_in_container`：要清理的目录（容器内路径）
- `keep_days`：保留天数（超过将删除）
- `interval_hours`：清理间隔（小时）

> 安全策略：仅删除常见图片扩展名（jpg/jpeg/png/webp/gif）。

### dedup（防重）
- `enabled`
- `db_path`：默认 `/session/dedup.sqlite3`
- `ttl_seconds`：可选；0 表示不过期

### rules.transforms（文本清洗流水线）
按顺序执行的 transforms。

内置 transforms：
- `regex_replace`：正则替换
- `append_dynamic`：追加模板；自动提取 `{title}`
- `filter_text`：过滤器（黑名单/白名单），丢弃则不转发

---

## 获取 Telegram api_id / api_hash

1. 打开 https://my.telegram.org 并登录
2. 进入 **API development tools**
3. 创建应用（如未创建过）
4. 复制页面显示的：
   - `api_id`
   - `api_hash`

---

## 安装/运行 NapCat（WebUI 配置说明）

### 步骤（首次）
1) 进入 `napcat/` 目录，准备 NapCat 需要的目录（会自动创建）：
   - `napcat/data/`（/AstrBot/data）
   - `napcat/ntqq/`（/app/.config/QQ）
   - `napcat/napcat/config/`（/app/napcat/config）

2) 启动 NapCat + tg2qqqun：
   - 先按 NapCat 官方要求设置 UID/GID（用于挂载目录权限）
   - 然后启动 compose

3) 打开 NapCat WebUI：
   - `http://<宿主机IP>:6099/webui`

4) 在 WebUI 启用 OneBot v11 HTTP Server（按截图一步步来）：
   1. 左侧菜单进入：**网络配置**
   2. 点击上方：**新建**
   3. 在弹出的类型列表中选择：**HTTP服务器**（不要选 HTTP 客户端 / Websocket）
   4. 在配置页面按如下填写/选择：
      - **启用**：打开
      - **名称**：随便填一个（例如：`tg2qqqun`）
      - **Host**：`0.0.0.0`
      - **Port**：`3000`
      - **消息格式**：选择 `Array`（本项目按消息段数组发送）
      - **Token**：设置一个强随机值（稍后要填到本项目 `config/config.yaml -> qq.token`）
      - （可选）**启用 CORS**：一般不需要；仅当你要在浏览器里直接跨域调用接口时才需要
      - （可选）**开启 Debug**：排障时可开启，会打印更多日志
      - （可选）**启用 Websocket**：本项目不需要，保持关闭
   5. 点击右下角：**保存**
   6. 回到网络配置列表，确认你刚新建的条目右侧开关保持为“启用”状态

5) 回到本项目 `config/config.yaml`，对应配置：
- `qq.onebot_base_url: "http://127.0.0.1:3000"`（与 NapCat HTTP Server 的端口一致）
- `qq.token: "<你在 WebUI 配置的 Token>"`

> 说明：该 compose 已把宿主机 `/home/tg2qqqun/data/tg_media` 挂载到 NapCat 容器内 `/AstrBot/data/tg_media:ro`，用于 `file://` 发图。

---

## 常见问题

### Q：如何同时监听多个 TG 频道，并转发到多个 QQ 群？
- 多频道：在 `telegram.sources` 下增加多条即可（每行一个）。
- 多群：在 `qq.group_ids` 中填多个群号即可。

### Q：如何确认已成功转发到所有群？
查看 `docker logs -f tg2qq`：
- 每条 TG 新消息会先出现：`incoming: ...`
- 发送成功会出现：`sent text: group_id=...` 或 `sent forward: group_id=...`
- 某个群失败会出现：`send ... failed: group_id=...`（但其它群仍会继续发送）

---

## 开源安全提示（强烈建议）

请勿提交敏感信息：
- `config/config.yaml` 中真实的 `api_id/api_hash`、NapCat token
- `session/telegram.session`
- `session/dedup.sqlite3`
- `data/tg_media/` 下的媒体文件

仓库已提供：
- `config/config.example.yaml`（示例配置）
- `.gitignore`（默认忽略 config/config.yaml、session、data 等）

---

## License

见 `LICENSE`。
