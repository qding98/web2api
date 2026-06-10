# Anything Web 反代适配说明

本文档记录本次对 `https://www.anything.com/` 网页端反代的调研结论、项目改动、运行配置、账号信息获取方式和当前边界。目标是让这个仓库在 Windows 本地和 Linux/Docker 环境中都能以 `type=anything` 的方式接入 Anything Web。

## 1. 当前结论

原项目只正式支持 Claude Web，不支持 Anything Web。

本次已经新增 `anything` 插件，并接入现有 Web2API 架构：

- OpenAI 路由：`POST /openai/anything/v1/chat/completions`
- OpenAI models：`GET /openai/anything/v1/models`
- Anthropic 兼容层理论上也能按 provider/type 路由，但 Anything 当前主要按 OpenAI chat completion 使用
- 支持真实浏览器上下文、账号池、代理组、会话缓存、零宽 session id 续聊机制

当前默认适配模式是：**在已有 Anything 项目中继续聊天/生成**。也就是说，需要提供 `projectGroupId`。自动创建新 Anything 项目、附件上传、WebSocket 实时订阅暂未实现。

## 2. Anything Web 页面结构与协议

调研来源：

- 官方入口：`https://www.anything.com/`
- 登录页：`https://www.anything.com/login`
- 前端静态 chunk：`https://www.anything.com/_next/static/chunks/51355-eb6e75466d626b1e.js`

观察到的结构：

- Anything 是 Next.js Web 应用。
- 主页和登录页可由真实浏览器正常访问，但普通后端 HTTP 抓取可能被 Vercel 安全检查拦截。
- 登录支持 Google、Apple、邮箱/手机号等方式。
- 前端 API base 在 Web 端表现为 `/api`。
- 主要业务接口是 GraphQL：
  - HTTP GraphQL：`/api/graphql`
  - WebSocket subscription：`/subscriptions`
- 登录态混合依赖：
  - 浏览器 cookie，例如可选的 `refresh_token`
  - localStorage token，例如 `authorization`、`access_token`、`accessToken`、`token`
  - GraphQL 请求中的 `authorization` header
- 前端存在 token 刷新接口：
  - `POST /api/refresh_token`

本次实现没有裸连后端接口，而是复用项目已有的 Playwright 页面，在页面内执行 `fetch`。这样可以复用真实浏览器的 cookie、localStorage、代理、TLS/指纹与站点脚本环境。

## 3. 关键 GraphQL 操作

前端 chunk 中观察到的核心操作包括：

- `GenerateProjectGroupRevisionFromChat`
- `EnqueueMessageForGeneration`
- `CreateProjectGroup`
- `CreateProjectGroupForNew`
- `ProjectGroupRevisionForChatById`
- `GetProjectGroupRevisionsForChat`
- `GetThreadsForProjectGroup`
- `GetProjectGroupByIdForAppBuilder`
- `GetProjectGroupRevisionContentUpdate`
- `QueuedMessageAccepted`
- `ProjectGroupRevisionsFinished`

本次实现使用的是最小闭环：

1. 调用 mutation `GenerateProjectGroupRevisionFromChat`
2. 获取 `projectGroupRevision.id`
3. 轮询 query `ProjectGroupRevisionForChatById`
4. 从 revision 的 `response` 字段提取文本
5. 根据 `status` 判断完成、失败或继续等待

使用轮询而不是 WebSocket 的原因：

- 现有项目插件抽象更容易接入 `AsyncIterator[str]` 文本流。
- 轮询不需要额外维护 subscription 连接。
- 对反代 API 来说，稳定性优先于前端级实时性。

## 4. 反代需要的信息

配置 Anything 账号时，至少需要：

```json
{
  "authorization": "Bearer 你的 Anything access token",
  "projectGroupId": "你的 Anything project group id"
}
```

推荐完整配置：

```json
{
  "authorization": "Bearer 你的 Anything access token",
  "refresh_token": "可选的 refresh_token cookie",
  "projectGroupId": "你的 Anything project group id",
  "threadId": "可选的 thread id"
}
```

可选高级字段：

```json
{
  "authorizationStorageKey": "accessToken",
  "authorizationStorageKeys": ["authorization", "accessToken"],
  "headers": {
    "x-custom-header": "value"
  },
  "generateInputTemplate": {
    "projectGroupId": "{project_group_id}",
    "threadId": "{thread_id}",
    "content": "{message}"
  }
}
```

字段含义：

| 字段 | 必需 | 说明 |
|---|---:|---|
| `authorization` | 是 | GraphQL 请求头，通常是 `Bearer eyJ...` |
| `projectGroupId` | 是 | Anything 项目组 ID，决定在哪个项目里继续生成 |
| `refresh_token` | 否 | Anything cookie，可帮助浏览器保留登录态 |
| `threadId` | 否 | 指定继续某个线程；不填时由 Anything 返回的新 thread 更新本地 session |
| `authorizationStorageKey` | 否 | token 写入 localStorage 的 key，当前默认会写多个候选 key |
| `headers` | 否 | 额外 GraphQL header |
| `generateInputTemplate` | 否 | Anything 前端 GraphQL input 字段变化时用来覆盖默认请求体 |

## 5. token 和 projectGroupId 获取方式

最稳方式是从你自己登录后的浏览器开发者工具中获取。

步骤：

1. 打开并登录 `https://www.anything.com/`。
2. 进入你要反代的项目。
3. 按 `F12` 打开开发者工具。
4. 进入 `Network` 面板。
5. 过滤关键词：`graphql`。
6. 在 Anything 聊天框发送一句测试消息。
7. 点开 `POST /api/graphql` 请求。

在请求中查找：

- `Headers -> Request Headers -> authorization`
  - 复制完整值，例如 `Bearer eyJ...`
- `Payload -> variables.input.projectGroupId`
  - 这个就是 `projectGroupId`
- `Payload -> variables.input.threadId`
  - 如果存在，可以作为可选 `threadId`

备用方式：

- DevTools -> `Application` -> `Local Storage` -> `https://www.anything.com`
  - 查找 `authorization`、`access_token`、`accessToken`、`token`
- DevTools -> `Application` -> `Cookies` -> `https://www.anything.com`
  - 查找 `refresh_token`

安全提醒：

- 不要把 token、cookie、抓包截图提交到仓库。
- 不要把这些值发到聊天窗口。
- 如果泄露，立即在 Anything 退出登录或刷新登录态。

## 6. 本次代码改动

新增文件：

- `core/plugin/anything.py`
  - 新增 `AnythingPlugin`
  - 编译账号 auth JSON
  - 写入 cookie/localStorage
  - 调用 Anything GraphQL mutation
  - 轮询 revision query
  - 把 `response` 做增量输出
- `tests/test_anything_plugin.py`
  - 覆盖 auth 解析、GraphQL payload/input 构造、revision 文本抽取、mutation + polling 增量输出
- `docs/anything-web-adapter.md`
  - 当前文档

修改文件：

- `core/app.py`
  - 启动时注册 `anything` 插件
- `core/constants.py`
  - 浏览器路径改为按系统自动探测
  - 支持 `WEB2API_CHROMIUM_BIN` 或 `CHROMIUM_BIN` 环境变量覆盖
  - Windows 默认查找 Chrome/Edge/fingerprint-chromium
  - Linux 默认查找 `/opt/fingerprint-chromium/chrome`、`/usr/bin/chromium` 等
- `config.yaml`
  - 增加 `anything:` 配置段
  - 根配置 `browser.chromium_bin` 改为空值，默认自动探测
- `docker/config.container.yaml`
  - 增加容器内 `anything:` 配置段
- `docs/config.md`
  - 增加 Anything 配置说明
  - 增加 Windows/Linux 浏览器路径说明
- `README.md`
  - 增加 Anything 账号配置和请求示例
- `README.en.md`
  - 同步英文说明

## 7. 默认配置

`config.yaml` 中新增：

```yaml
anything:
  start_url: ''
  api_base: ''
  graphql_path: '/graphql'
  default_project_group_id: ''
  default_thread_id: ''
  authorization_storage_keys:
    - authorization
    - access_token
    - accessToken
    - token
  generate_input_template: {}
  request_timeout_seconds: 30
  poll_interval_seconds: 2
  poll_timeout_seconds: 180
  rate_limit_fallback_cooldown_seconds: 60
  yield_empty_on_accepted: true
  clear_existing_cookies: false
  model_mapping:
    anything: anything-web
```

默认 URL：

- `start_url`: `https://www.anything.com`
- `api_base`: `https://www.anything.com/api`
- `graphql_path`: `/graphql`

## 8. Windows 本地运行

推荐：

```powershell
uv sync
uv run python main.py
```

浏览器路径优先级：

1. `config.yaml -> browser.chromium_bin`
2. `WEB2API_CHROMIUM_BIN`
3. `CHROMIUM_BIN`
4. Windows 常见安装路径：
   - `C:\Program Files\fingerprint-chromium\chrome.exe`
   - `C:\Program Files\Google\Chrome\Application\chrome.exe`
   - `C:\Program Files\Microsoft\Edge\Application\msedge.exe`

如果自动探测不对，可以显式配置：

```yaml
browser:
  chromium_bin: 'C:\Program Files\fingerprint-chromium\chrome.exe'
```

## 9. Linux/Docker 运行

Linux 裸机建议使用 Xvfb：

```bash
sudo apt install -y xvfb
xvfb-run -a -s "-screen 0 1920x1080x24" uv run python main.py
```

Docker 配置文件 `docker/config.container.yaml` 保留：

```yaml
browser:
  chromium_bin: '/opt/fingerprint-chromium/chrome'
  no_sandbox: true
  disable_gpu: true
  disable_gpu_sandbox: true
```

Anything 配置段已同步加入 Docker 配置。

## 10. 请求示例

配置页新增账号：

- `type`: `anything`
- `auth`: 填前文 JSON

请求：

```bash
curl -s "http://127.0.0.1:9000/openai/anything/v1/chat/completions" \
  -H "Authorization: Bearer your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "anything", "stream": false, "messages": [{"role":"user","content":"继续完善这个 app 的登录页"}]}'
```

如果没有配置 `auth.api_key`，可以去掉 `Authorization` 请求头。

## 11. 已验证内容

本次本地验证命令：

```bash
uv run ruff check core\constants.py core\plugin\anything.py core\app.py tests\test_anything_plugin.py
uv run python -m unittest discover -s tests
```

验证结果：

- Ruff 通过
- 全量单元测试通过：`49 tests OK`
- 插件注册验证输出：`claude,anything`

没有执行真实 Anything 端到端请求，因为当前环境没有你的 Anything 登录 token 和 `projectGroupId`。真实端到端验证需要你在本机配置账号 auth JSON 后发起 `/openai/anything/v1/chat/completions` 请求。

## 12. 当前边界与后续方向

当前已支持：

- 使用真实浏览器页面调用 Anything GraphQL
- 使用 `authorization` header 与可选 cookie/localStorage 恢复登录态
- 在已有 `projectGroupId` 项目中发起生成
- 轮询 revision 并输出 `response`
- 按 Anything 429 冻结账号一段时间，复用项目现有限流调度
- Windows/Linux/Docker 浏览器路径适配

暂未支持：

- 自动登录 Anything
- 自动提取 token/projectGroupId
- 自动创建新 Anything 项目
- 附件/图片上传
- WebSocket subscription 实时流
- 显式调用 `/api/refresh_token` 刷新 access token

如果后续要进一步增强，优先级建议：

1. 用你的真实账号抓包验证默认 `GenerateProjectGroupRevisionFromChatInput` 字段是否完全匹配。
2. 如果字段不匹配，先用 `generateInputTemplate` 覆盖，不急着改代码。
3. 增加自动创建项目逻辑，对接 `CreateProjectGroup` 或 `CreateProjectGroupForNew`。
4. 增加 WebSocket subscription，减少轮询延迟。
5. 增加 token refresh 流程，减少 access token 过期导致的失败。
