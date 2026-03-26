# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

tg2qqqun 是一个 Telegram 到 QQ 群的消息转发工具，基于：
- Telegram 客户端：Telethon
- QQ 机器人：NapCat OneBot v11 HTTP API
- 部署方式：Docker Compose

核心功能：监听指定 Telegram 公开频道的新消息，经过文本清洗和过滤后，转发到一个或多个 QQ 群。

## 核心架构

### 主要模块

- `app/main.py` - 主程序入口，协调 Telegram 监听和 QQ 转发
- `app/qq_onebot.py` - OneBot v11 HTTP 客户端，封装 NapCat API 调用
- `app/transforms.py` - 消息变换器（正则替换、动态追加、过滤器）
- `app/rule_engine.py` - 规则引擎，按配置顺序执行 transforms
- `app/dedup_store.py` - SQLite 持久化去重存储
- `app/media_cleanup.py` - 定期清理过期媒体文件
- `app/login.py` - Telegram 首次登录工具（生成 session）

### 数据流

```
Telegram 频道新消息
  ↓
Telethon 事件监听 (main.py)
  ↓
去重检查 (dedup_store.py)
  ↓
文本清洗 (transforms.py + rule_engine.py)
  ↓
图片下载到宿主机 (如有)
  ↓
发送到多个 QQ 群 (qq_onebot.py)
  ↓
标记去重 (成功后)
```

### 关键设计

1. **多群转发**：`qq.group_ids` 支持配置多个群号，单群失败不影响其他群
2. **发送策略**：优先普通图文消息，失败则降级为合并转发（更稳定）
3. **去重机制**：基于 `chat_id + msg_id`，支持两种标记时机：
   - `success`（默认）：至少一个群发送成功后标记
   - `receive`：接收消息即标记
4. **重试策略**：OneBot 调用支持指数退避重试（可配置）
5. **媒体管理**：图片落盘到宿主机，NapCat 通过 `file://` 读取；定期清理过期文件

## 常用命令

### 开发和测试

```bash
# 首次登录 Telegram（生成 session）
docker compose run --rm tg2qq python login.py

# 启动转发器
docker compose up -d --build

# 查看日志
docker logs -f tg2qqQun

# 停止服务
docker compose down

# 重启服务
docker compose restart tg2qq
```

### NapCat 组合部署

```bash
# 进入 napcat 目录
cd napcat

# 设置 UID/GID（首次）
echo "NAPCAT_UID=$(id -u)" > .env
echo "NAPCAT_GID=$(id -g)" >> .env

# 启动 NapCat + tg2qqqun
docker compose up -d --build

# 查看两个容器日志
docker logs -f napcat
docker logs -f tg2qq
```

## 配置说明

配置文件：`config/config.yaml`

### 关键配置项

- `telegram.sources`：监听来源列表，支持 `@username` 或 `https://t.me/xxx`
- `qq.group_ids`：目标 QQ 群号列表（必填，非空）
- `qq.onebot_base_url`：NapCat OneBot HTTP 地址
- `qq.token`：NapCat HTTP 认证 token
- `qq.send_interval_seconds`：每条消息最小发送间隔（防风控）
- `storage.host_media_dir_in_container`：tg2qq 容器内图片保存路径
- `storage.napcat_media_dir_in_container`：NapCat 容器内图片读取路径
- `dedup.mark_on`：去重标记时机（`success` 或 `receive`）
- `rules.transforms`：文本清洗流水线（按顺序执行）

### Transform 类型

- `regex_replace`：正则替换（`pattern` + `repl`）
- `append_dynamic`：追加模板，自动提取 `{title}` 占位符
- `filter_text`：过滤器，支持黑名单/白名单（关键词 + 正则）

## 重要约束

1. **路径映射一致性**：
   - tg2qq 下载图片到 `host_media_dir_in_container`
   - NapCat 必须能通过 `napcat_media_dir_in_container` 访问同一目录
   - 两个路径必须映射到宿主机同一目录

2. **NapCat 配置要求**：
   - 必须启用 OneBot v11 HTTP Server
   - 消息格式必须选择 `Array`（消息段数组）
   - Token 必须与 `qq.token` 一致

3. **Telegram 账号要求**：
   - 必须已加入要监听的频道/群
   - 需要有效的 `api_id` 和 `api_hash`

4. **去重数据库**：
   - 位于 `session/dedup.sqlite3`
   - 容器重启后仍然有效（通过 volume 持久化）

## 故障排查

### Telegram 连接失败
- 检查 `api_id` / `api_hash` 是否正确
- 确认 `session/telegram.session` 已生成
- 查看是否需要重新运行 `login.py`

### QQ 发送失败
- 检查 NapCat OneBot HTTP Server 是否启用
- 确认 `onebot_base_url` 和 `token` 配置正确
- 查看 NapCat 日志：`docker logs -f napcat`

### 图片发送失败（rich media transfer failed）
- 确认路径映射正确（宿主机 → tg2qq 容器 → NapCat 容器）
- 检查 NapCat 是否有读取权限（`:ro` 挂载）
- 查看是否触发降级为合并转发

### 重复转发
- 检查 `dedup.enabled` 是否为 `true`
- 确认 `session/dedup.sqlite3` 未被删除
- 查看 `dedup.mark_on` 配置是否符合预期

## 扩展开发

### 添加新的 Transform

1. 在 `app/transforms.py` 中定义函数：
   ```python
   def my_transform(m: Msg, **kwargs) -> Msg:
       # 处理逻辑
       return m
   ```

2. 注册到 `TRANSFORM_MAP`：
   ```python
   TRANSFORM_MAP = {
       # ...
       "my_transform": my_transform,
   }
   ```

3. 在 `config/config.yaml` 中使用：
   ```yaml
   rules:
     transforms:
       - type: "my_transform"
         param1: value1
   ```

### 修改发送逻辑

主要逻辑在 `main.py` 的 `handler` 函数中：
- 图片消息：先尝试 `send_group_image_text`，失败则降级为 `send_group_forward`
- 纯文本：直接调用 `send_group_text`
- 多群发送：遍历 `group_ids`，单群失败不影响其他群

### 调整重试策略

在 `config/config.yaml` 中配置 `qq.retry`：
- `enabled`：是否启用重试
- `max_attempts`：最大尝试次数
- `base_delay_ms`：基础延迟（指数退避）
- `max_delay_ms`：最大延迟上限
- `jitter_ms`：随机抖动（降低重试风暴）
