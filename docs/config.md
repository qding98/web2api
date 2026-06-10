# 配置说明

项目根目录的 [config.yaml](../config.yaml) 主要控制：

- 服务端口
- API Key 鉴权
- 配置页登录密钥
- 浏览器可执行文件路径
- 调度与回收参数
- mock 调试端口

## 配置文件优先级

如果你本地经常需要改配置、又不想每次提交都处理 `config.yaml` 的变更，可以在项目根目录新建 `config.local.yaml`。

加载优先级：

1. `WEB2API_CONFIG_PATH` 环境变量
2. `config.local.yaml`
3. `config.yaml`

## 关键配置项

### 服务端口

```yaml
server:
  port: 9000
```

### 浏览器路径

本地运行时可以留空让程序按系统自动探测，也可以显式指定：

```yaml
browser:
  chromium_bin: ''
```

Windows 示例：`C:\Program Files\fingerprint-chromium\chrome.exe`

Linux 示例：`/usr/bin/chromium` 或 `/opt/fingerprint-chromium/chrome`

也可以用环境变量覆盖：`WEB2API_CHROMIUM_BIN=/path/to/chrome`

### Linux / Docker 兼容参数

如果浏览器在 Linux / Docker 环境里启动后立刻关闭，可以尝试：

```yaml
browser:
  no_sandbox: true
  disable_gpu: true
  disable_gpu_sandbox: true
```

注意：这更适合容器、Xvfb、远程桌面环境；对本机桌面环境通常不需要。

### 下载目录

默认下载目录会落在每个浏览器实例的 `user-data-dir` 下，形如：

`~/fp-data/<fingerprint_id>/downloads`

也可以在 `config.yaml` 里自定义（会覆盖默认值）：

```yaml
browser:
  download_dir: ~/Downloads
```

建议使用绝对路径或以 `~` 开头的路径。

### API Key 鉴权

如果不希望任何人拿到地址就能直接调用，建议配置：

````yaml
启用后：

- `/{type}/v1/*` 都需要带其中一个有效 key
- 推荐请求头：`Authorization: Bearer your-secret-key`
- 修改 `auth.api_key` 后需要重启服务

### 配置页保护

如果要保护配置页面：

```yaml
auth:
  config_secret: '配置页面登录密码'
````

行为说明：

- 如果 `config_secret` 留空，`/config` 与 `/api/config` 不可访问
- 如果填的是明文，项目启动后会自动转换成哈希并回写到当前 `config.yaml`
- 以后访问配置页面时，需要先打开 `/login`，输入这个明文 secret 登录
- 如果要改 secret，直接把 `config_secret` 改成新的明文，再重启服务即可
- 配置页登录默认按来源 IP 做简单限流：连续失败 5 次后锁定 600 秒，可通过 `auth.config_login_max_failures` 和 `auth.config_login_lock_seconds` 调整

### Anything Web 配置

Anything 插件通过真实浏览器页调用 `https://www.anything.com/api/graphql`。当前默认适配“已有项目继续对话”，因此需要知道 Anything 的 `projectGroupId`。

全局兜底配置：

```yaml
anything:
  start_url: ''
  api_base: ''
  graphql_path: '/graphql'
  default_project_group_id: ''
  default_thread_id: ''
  poll_interval_seconds: 2
  poll_timeout_seconds: 180
  model_mapping:
    anything: anything-web
```

推荐在配置页账号 auth JSON 中按账号填写：

```json
{
  "authorization": "Bearer 你的 Anything access token",
  "refresh_token": "可选的 refresh_token cookie",
  "projectGroupId": "你的 Anything project group id",
  "threadId": "可选的 thread id"
}
```

如果 Anything 前端版本更新导致 GraphQL input 字段变动，可以用 `generateInputTemplate` 覆盖默认 input。模板支持 `{message}`、`{project_group_id}`、`{thread_id}`、`{timezone}`、`{session_id}` 等占位符：

```json
{
  "authorization": "Bearer 你的 Anything access token",
  "projectGroupId": "你的 Anything project group id",
  "generateInputTemplate": {
    "projectGroupId": "{project_group_id}",
    "threadId": "{thread_id}",
    "content": "{message}"
  }
}
```

请求路由：

```bash
POST /openai/anything/v1/chat/completions
GET  /openai/anything/v1/models
```

完整接入说明见 [Anything Web 反代适配说明](anything-web-adapter.md)。
