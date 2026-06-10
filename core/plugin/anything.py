"""
Anything Web 插件：把 https://www.anything.com/ 的网页端 GraphQL 生成能力接入 Web2API。

本脚本在项目中的作用：
- 由 core.app 在启动时注册为 type=anything 的插件。
- 复用 core.plugin.helpers 中的真实浏览器 page.fetch 能力发起 /api/graphql 请求。
- 输入来源包括 config.yaml 的 anything 配置、配置页账号 auth JSON、OpenAI/Anthropic 兼容请求的用户消息。
- 输出内容是 Anything 生成回复的文本增量，由 core.api.chat_handler 包装成兼容协议流。
- 对外提供 register_anything_plugin()、AnythingPlugin 以及若干可单测的构造/解析函数。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator

from playwright.async_api import BrowserContext, Page

from core.config.settings import get, get_bool, get_float
from core.constants import TIMEZONE
from core.plugin.base import AbstractPlugin, PluginRegistry
from core.plugin.errors import AccountFrozenError
from core.plugin.helpers import (
    clear_cookies_for_domain,
    clear_page_storage_for_switch,
    create_page_for_site,
    request_json_via_page_fetch,
    safe_page_reload,
)

logger = logging.getLogger(__name__)


ANYTHING_DEFAULT_START_URL = "https://www.anything.com"
ANYTHING_DEFAULT_API_BASE = "https://www.anything.com/api"
ANYTHING_COOKIE_DOMAIN = ".anything.com"
ANYTHING_GRAPHQL_PATH = "/graphql"

DEFAULT_AUTHORIZATION_STORAGE_KEYS = [
    "authorization",
    "access_token",
    "accessToken",
    "token",
]

SUCCESS_TERMINAL_STATUSES = {
    "COMPLETED",
    "COMPLETE",
    "FINISHED",
    "READY",
    "SUCCEEDED",
    "SUCCESS",
    "DONE",
}

FAILED_TERMINAL_STATUSES = {
    "FAILED",
    "ERROR",
    "CANCELED",
    "CANCELLED",
    "TIMEOUT",
    "TIMED_OUT",
}

GENERATE_REVISION_MUTATION = """
mutation GenerateProjectGroupRevisionFromChat($input: GenerateProjectGroupRevisionFromChatInput!) {
  generateProjectGroupRevisionFromChat(input: $input) {
    success
    askForUserProfileInfo
    errors {
      kind
      message
    }
    projectGroupRevision {
      id
      response
      status
      createdAt
      thread {
        id
        title
        updatedAt
      }
    }
  }
}
"""

POLL_REVISION_QUERY = """
query ProjectGroupRevisionForChatById($id: ID!) {
  projectGroupRevisionById(id: $id) {
    id
    response
    status
    createdAt
    thread {
      id
      title
      updatedAt
    }
    queuedMessages {
      id
      content
      status
    }
  }
}
"""


@dataclass(frozen=True)
class AnythingAuthSettings:
    """单个 Anything 账号在浏览器页和 GraphQL 请求中需要的认证与会话参数。"""

    authorization_header: str | None
    local_storage_value: str | None
    local_storage_keys: list[str]
    cookies: list[dict[str, Any]]
    extra_headers: dict[str, str]
    project_group_id: str | None
    thread_id: str | None
    generate_input_template: Any | None


def _clean_str(value: Any) -> str | None:
    """将任意输入规整为非空字符串；输入来自 YAML 配置或账号 auth JSON，输出供 URL、header、ID 字段使用。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _strip_trailing_slash(value: str) -> str:
    """去掉 URL 末尾斜杠；输入为配置 URL，输出供路径拼接使用。"""
    return value.rstrip("/")


def _config_str(key: str, default: str) -> str:
    """读取 anything 配置字符串；输入来自 config.yaml，输出为带默认值的字符串。"""
    value = _clean_str(get("anything", key))
    return value if value is not None else default


def _config_list(key: str, default: list[str]) -> list[str]:
    """读取 anything 配置列表；输入来自 config.yaml，输出为字符串列表。"""
    value = get("anything", key)
    if isinstance(value, list):
        items = [_clean_str(item) for item in value]
        return [item for item in items if item]
    if isinstance(value, str):
        items = [_clean_str(item) for item in value.split(",")]
        return [item for item in items if item]
    return list(default)


def _config_mapping(key: str) -> dict[str, Any]:
    """读取 anything 配置映射；输入来自 config.yaml，输出为 dict，非法时返回空 dict。"""
    value = get("anything", key)
    return dict(value) if isinstance(value, dict) else {}


def _first_non_empty(auth: dict[str, Any], keys: list[str]) -> str | None:
    """按候选 key 从账号 auth 中取第一个非空值；输出用于 token、项目 ID 或线程 ID。"""
    for key in keys:
        value = _clean_str(auth.get(key))
        if value:
            return value
    return None


def _has_auth_scheme(value: str) -> bool:
    """判断 Authorization 值是否已经带有认证 scheme；输入为 token 字符串，输出为布尔值。"""
    lowered = value.lower()
    return lowered.startswith("bearer ") or lowered.startswith("basic ")


def _authorization_header_from_auth(auth: dict[str, Any]) -> str | None:
    """从账号 auth 推导 GraphQL Authorization header；输出可直接放入 fetch headers。"""
    explicit = _first_non_empty(auth, ["authorization", "Authorization"])
    if explicit:
        return explicit
    token = _first_non_empty(auth, ["accessToken", "access_token", "token"])
    if not token:
        return None
    prefix = _clean_str(auth.get("authorizationPrefix"))
    if prefix is None:
        prefix = "Bearer "
    return token if not prefix else f"{prefix}{token}"


def _local_storage_token_from_auth(auth: dict[str, Any]) -> str | None:
    """从账号 auth 推导写入 localStorage 的 token；输出供页面前端恢复登录态。"""
    explicit = _first_non_empty(auth, ["localStorageToken", "accessToken", "access_token", "token"])
    if explicit:
        return explicit
    authorization = _first_non_empty(auth, ["authorization", "Authorization"])
    if authorization and _has_auth_scheme(authorization):
        return authorization.split(" ", 1)[1].strip()
    return authorization


def _storage_keys_from_auth(auth: dict[str, Any]) -> list[str]:
    """合并账号 auth 与配置中的 localStorage key；输出为去重后的 key 列表。"""
    configured = _config_list("authorization_storage_keys", DEFAULT_AUTHORIZATION_STORAGE_KEYS)
    raw = auth.get("authorizationStorageKeys")
    if isinstance(raw, list):
        configured = [_clean_str(item) for item in raw] + configured
    single = _clean_str(auth.get("authorizationStorageKey"))
    if single:
        configured.insert(0, single)
    seen: set[str] = set()
    result: list[str] = []
    for item in configured:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _normalize_cookie(cookie: dict[str, Any]) -> dict[str, Any] | None:
    """把账号 auth 中的 cookie 项规整为 Playwright add_cookies 参数；非法项返回 None。"""
    name = _clean_str(cookie.get("name"))
    value = _clean_str(cookie.get("value"))
    if not name or value is None:
        return None
    return {
        "name": name,
        "value": value,
        "domain": _clean_str(cookie.get("domain")) or ANYTHING_COOKIE_DOMAIN,
        "path": _clean_str(cookie.get("path")) or "/",
        "secure": bool(cookie.get("secure", True)),
        "httpOnly": bool(cookie.get("httpOnly", cookie.get("http_only", False))),
    }


def _cookies_from_auth(auth: dict[str, Any]) -> list[dict[str, Any]]:
    """从账号 auth 读取 refresh token 与 cookies；输出供 BrowserContext.add_cookies 使用。"""
    cookies: list[dict[str, Any]] = []
    refresh_token = _first_non_empty(auth, ["refresh_token", "refreshToken"])
    if refresh_token:
        cookies.append(
            {
                "name": "refresh_token",
                "value": refresh_token,
                "domain": ANYTHING_COOKIE_DOMAIN,
                "path": "/",
                "secure": True,
                "httpOnly": True,
            }
        )
    raw_cookies = auth.get("cookies")
    if isinstance(raw_cookies, dict):
        raw_cookies = [
            {"name": str(name), "value": value}
            for name, value in raw_cookies.items()
        ]
    if isinstance(raw_cookies, list):
        for item in raw_cookies:
            if isinstance(item, dict):
                normalized = _normalize_cookie(item)
                if normalized:
                    cookies.append(normalized)
    return cookies


def _extra_headers_from_auth(auth: dict[str, Any]) -> dict[str, str]:
    """读取账号 auth 中的额外 GraphQL header；输出会合并到页面 fetch headers。"""
    headers: dict[str, str] = {}
    raw = auth.get("headers")
    if isinstance(raw, dict):
        for key, value in raw.items():
            clean_key = _clean_str(key)
            clean_value = _clean_str(value)
            if clean_key and clean_value is not None:
                headers[clean_key] = clean_value
    return headers


def _generate_template_from_sources(auth: dict[str, Any]) -> Any | None:
    """读取生成 mutation 的 input 模板；账号 auth 优先，其次 config.yaml。"""
    if "generateInputTemplate" in auth:
        return copy.deepcopy(auth.get("generateInputTemplate"))
    configured = _config_mapping("generate_input_template")
    return configured or None


def build_anything_auth_settings(auth: dict[str, Any]) -> AnythingAuthSettings:
    """把账号 auth JSON 编译成 AnythingAuthSettings；供 apply_auth 和 create_conversation 共享。"""
    return AnythingAuthSettings(
        authorization_header=_authorization_header_from_auth(auth),
        local_storage_value=_local_storage_token_from_auth(auth),
        local_storage_keys=_storage_keys_from_auth(auth),
        cookies=_cookies_from_auth(auth),
        extra_headers=_extra_headers_from_auth(auth),
        project_group_id=_first_non_empty(
            auth,
            ["projectGroupId", "project_group_id", "projectId", "project_id"],
        ),
        thread_id=_first_non_empty(auth, ["threadId", "thread_id"]),
        generate_input_template=_generate_template_from_sources(auth),
    )


def build_graphql_payload(
    operation_name: str,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    """构建 GraphQL POST JSON；输入为操作名、文档和变量，输出供 page.fetch 使用。"""
    return {
        "operationName": operation_name,
        "query": query,
        "variables": variables,
    }


def _render_template_value(value: Any, replacements: dict[str, str]) -> Any:
    """递归渲染 input 模板中的占位符；输入为模板片段，输出为渲染后的同构数据。"""
    if isinstance(value, str):
        rendered = value
        for key, replacement in replacements.items():
            rendered = rendered.replace("{" + key + "}", replacement)
        return rendered
    if isinstance(value, list):
        return [_render_template_value(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _render_template_value(item, replacements)
            for key, item in value.items()
        }
    return value


def _drop_empty_values(value: Any) -> Any:
    """递归删除模板渲染后的空字符串、None、空 dict/list；输出适合发给 GraphQL。"""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            cleaned = _drop_empty_values(item)
            if cleaned not in (None, "", [], {}):
                result[key] = cleaned
        return result
    if isinstance(value, list):
        return [
            cleaned
            for item in value
            if (cleaned := _drop_empty_values(item)) not in (None, "", [], {})
        ]
    return value


def build_generate_input(
    *,
    message: str,
    project_group_id: str,
    thread_id: str | None,
    timezone: str,
    session_id: str,
    template: Any | None = None,
) -> dict[str, Any]:
    """构建 GenerateProjectGroupRevisionFromChatInput；输入来自会话状态和用户消息。"""
    if template is None:
        body: dict[str, Any] = {
            "projectGroupId": project_group_id,
            "content": message,
        }
        if thread_id:
            body["threadId"] = thread_id
        return body

    replacements = {
        "message": message,
        "project_group_id": project_group_id,
        "projectGroupId": project_group_id,
        "thread_id": thread_id or "",
        "threadId": thread_id or "",
        "timezone": timezone,
        "session_id": session_id,
    }
    rendered = _render_template_value(copy.deepcopy(template), replacements)
    cleaned = _drop_empty_values(rendered)
    if not isinstance(cleaned, dict):
        raise RuntimeError("Anything generateInputTemplate 渲染后必须是 JSON object")
    return cleaned


def extract_revision_text(revision: dict[str, Any] | None) -> str:
    """从 Anything revision 中提取回复文本；输入为 GraphQL revision 节点，输出为可流式返回的文本。"""
    if not isinstance(revision, dict):
        return ""
    response = revision.get("response")
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    return json.dumps(response, ensure_ascii=False)


def _extract_graphql_errors(data: Any) -> str | None:
    """解析 GraphQL errors 字段；输入为响应 JSON，输出为可读错误字符串。"""
    if not isinstance(data, dict):
        return None
    errors = data.get("errors")
    if not isinstance(errors, list) or not errors:
        return None
    messages: list[str] = []
    for item in errors:
        if isinstance(item, dict):
            messages.append(str(item.get("message") or item))
        else:
            messages.append(str(item))
    return "; ".join(messages)


def _extract_mutation_result(data: dict[str, Any]) -> dict[str, Any]:
    """从生成 mutation 响应中取业务结果；输入为 GraphQL JSON，输出为 generateProjectGroupRevisionFromChat 节点。"""
    payload = data.get("data")
    if not isinstance(payload, dict):
        raise RuntimeError("Anything GraphQL 未返回 data")
    result = payload.get("generateProjectGroupRevisionFromChat")
    if not isinstance(result, dict):
        raise RuntimeError("Anything GraphQL 未返回生成结果")
    errors = result.get("errors")
    if errors:
        raise RuntimeError(f"Anything 生成失败: {json.dumps(errors, ensure_ascii=False)}")
    if result.get("success") is False:
        raise RuntimeError("Anything 生成失败: success=false")
    return result


def _extract_revision_from_query(data: dict[str, Any]) -> dict[str, Any] | None:
    """从轮询 query 响应中取 revision；输入为 GraphQL JSON，输出为 revision 节点或 None。"""
    payload = data.get("data")
    if not isinstance(payload, dict):
        return None
    revision = payload.get("projectGroupRevisionById")
    return revision if isinstance(revision, dict) else None


def _normalized_status(revision: dict[str, Any] | None) -> str:
    """规整 Anything revision status；输入为 revision 节点，输出为大写状态字符串。"""
    if not isinstance(revision, dict):
        return ""
    status = _clean_str(revision.get("status"))
    return status.upper() if status else ""


def _is_failed_status(status: str) -> bool:
    """判断状态是否为失败终态；输入为大写 status，输出布尔值。"""
    return status in FAILED_TERMINAL_STATUSES


def _is_success_status(status: str) -> bool:
    """判断状态是否为成功终态；输入为大写 status，输出布尔值。"""
    return status in SUCCESS_TERMINAL_STATUSES


class AnythingPlugin(AbstractPlugin):
    """Anything Web2API 插件，使用浏览器内 GraphQL 请求创建生成任务并轮询 revision。"""

    type_name = "anything"

    def __init__(self) -> None:
        """初始化插件状态；维护 page 级认证状态和 session 级 Anything 项目状态。"""
        super().__init__()
        self._page_auth_state: dict[int, AnythingAuthSettings] = {}

    @property
    def start_url(self) -> str:
        """返回 Anything 页面入口 URL；输入来自 config.yaml，输出供 create_page/apply_auth 打开页面。"""
        return _config_str("start_url", ANYTHING_DEFAULT_START_URL)

    @property
    def api_base(self) -> str:
        """返回 Anything API base URL；输入来自 config.yaml，输出供 GraphQL URL 拼接。"""
        return _strip_trailing_slash(_config_str("api_base", ANYTHING_DEFAULT_API_BASE))

    @property
    def graphql_url(self) -> str:
        """返回 GraphQL 完整 URL；输入来自 api_base 与 graphql_path 配置。"""
        path = _config_str("graphql_path", ANYTHING_GRAPHQL_PATH)
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.api_base}{path}"

    def model_mapping(self) -> dict[str, str] | None:
        """返回 OpenAI 兼容模型别名；输入来自 config.yaml，输出供 /v1/models 使用。"""
        mapping = _config_mapping("model_mapping")
        if mapping:
            return {str(key): str(value) for key, value in mapping.items()}
        return {"anything": "anything-web"}

    async def create_page(
        self,
        context: BrowserContext,
        reuse_page: Page | None = None,
    ) -> Page:
        """创建或复用 Anything 页面；输入为浏览器 context/page，输出为可执行 fetch 的 Page。"""
        return await create_page_for_site(context, self.start_url, reuse_page=reuse_page)

    async def apply_auth(
        self,
        context: BrowserContext,
        page: Page,
        auth: dict[str, Any],
        *,
        reload: bool = True,
        **kwargs: Any,
    ) -> None:
        """把账号 auth 写入浏览器上下文；输入为配置页 auth JSON，输出为页面登录态和缓存的 auth settings。"""
        del kwargs
        settings = build_anything_auth_settings(auth)
        self._page_auth_state[id(page)] = settings
        await safe_page_reload(page, url=self.start_url)
        await clear_page_storage_for_switch(page)
        if get_bool("anything", "clear_existing_cookies", False):
            await clear_cookies_for_domain(context, ANYTHING_COOKIE_DOMAIN)
        if settings.cookies:
            await context.add_cookies(settings.cookies)
        if settings.local_storage_value:
            await self._write_local_storage_tokens(page, settings)
        if reload:
            await safe_page_reload(page)

    async def create_conversation(
        self,
        context: BrowserContext,
        page: Page,
        **kwargs: Any,
    ) -> str | None:
        """创建 Web2API 本地会话；输入为已认证页面，输出为合成 session_id 并保存项目/thread 状态。"""
        del context
        settings = self._page_auth_state.get(id(page))
        project_group_id = self._resolve_project_group_id(settings)
        if not project_group_id:
            raise RuntimeError(
                "Anything 需要在账号 auth JSON 或 config.yaml 中配置 projectGroupId。"
                "当前版本默认适配已有项目续写模式。"
            )
        session_id = f"anything-{uuid.uuid4().hex}"
        timezone = str(kwargs.get("timezone") or TIMEZONE)
        self._session_state[session_id] = {
            "project_group_id": project_group_id,
            "thread_id": self._resolve_thread_id(settings),
            "headers": self._headers_for_request(settings),
            "generate_input_template": settings.generate_input_template if settings else None,
            "timezone": timezone,
        }
        logger.info(
            "[anything] create_conversation session_id=%s project_group_id=%s thread_id=%s",
            session_id,
            project_group_id,
            self._session_state[session_id].get("thread_id") or "",
        )
        return session_id

    async def stream_completion(
        self,
        context: BrowserContext,
        page: Page,
        session_id: str,
        message: str,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """向 Anything 发起生成并轮询输出；输入为本地 session 与用户消息，输出为文本增量流。"""
        del context
        state = self._session_state.get(session_id)
        if not state:
            raise RuntimeError(f"未知 Anything 会话 ID: {session_id}")
        if kwargs.get("attachments"):
            raise RuntimeError("Anything 插件当前未实现附件上传，请先发送纯文本请求")

        revision_id = await self._start_generation(page, session_id, message, state)
        if get_bool("anything", "yield_empty_on_accepted", True):
            yield ""
        async for chunk in self._poll_revision(page, session_id, revision_id, state):
            yield chunk

    async def _write_local_storage_tokens(
        self,
        page: Page,
        settings: AnythingAuthSettings,
    ) -> None:
        """把 token 写入 Anything 页面 localStorage；输入为 auth settings，输出为浏览器端存储状态。"""
        items = {
            key: settings.local_storage_value
            for key in settings.local_storage_keys
            if settings.local_storage_value is not None
        }
        if not items:
            return
        await page.evaluate(
            """({ items }) => {
              for (const [key, value] of Object.entries(items)) {
                window.localStorage.setItem(key, value);
              }
            }""",
            {"items": items},
        )

    def _resolve_project_group_id(
        self,
        settings: AnythingAuthSettings | None,
    ) -> str | None:
        """解析 projectGroupId；输入为 page auth settings 与 config.yaml，输出为 Anything 项目组 ID。"""
        if settings and settings.project_group_id:
            return settings.project_group_id
        return _clean_str(get("anything", "default_project_group_id"))

    def _resolve_thread_id(
        self,
        settings: AnythingAuthSettings | None,
    ) -> str | None:
        """解析 threadId；输入为 page auth settings 与 config.yaml，输出为 Anything 线程 ID 或 None。"""
        if settings and settings.thread_id:
            return settings.thread_id
        return _clean_str(get("anything", "default_thread_id"))

    def _headers_for_request(
        self,
        settings: AnythingAuthSettings | None,
    ) -> dict[str, str]:
        """构建 GraphQL 请求头；输入为 auth settings，输出供 request_json_via_page_fetch 使用。"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if settings:
            headers.update(settings.extra_headers)
            if settings.authorization_header:
                headers["authorization"] = settings.authorization_header
        return headers

    async def _request_graphql(
        self,
        page: Page,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """通过页面 fetch 调用 Anything GraphQL；输入为 payload/headers，输出为解析后的 JSON。"""
        timeout_ms = int(max(1000, get_float("anything", "request_timeout_seconds", 30.0) * 1000))
        resp = await request_json_via_page_fetch(
            page,
            self.graphql_url,
            method="POST",
            body=json.dumps(payload, ensure_ascii=False),
            headers=headers,
            timeout_ms=timeout_ms,
        )
        status = int(resp.get("status") or 0)
        if status == 429:
            cooldown = get_float("anything", "rate_limit_fallback_cooldown_seconds", 60.0)
            raise AccountFrozenError("Anything 触发 429 限流", int(time.time() + cooldown))
        if status < 200 or status >= 300:
            text = str(resp.get("text") or "")[:800]
            raise RuntimeError(f"Anything GraphQL HTTP {status}: {text}")
        data = resp.get("json")
        if not isinstance(data, dict):
            text = str(resp.get("text") or "")[:800]
            raise RuntimeError(f"Anything GraphQL 返回非 JSON: {text}")
        errors = _extract_graphql_errors(data)
        if errors:
            raise RuntimeError(f"Anything GraphQL errors: {errors}")
        return data

    async def _start_generation(
        self,
        page: Page,
        session_id: str,
        message: str,
        state: dict[str, Any],
    ) -> str:
        """创建 Anything 生成任务；输入为用户消息和会话状态，输出为 revision_id。"""
        input_body = build_generate_input(
            message=message,
            project_group_id=str(state["project_group_id"]),
            thread_id=state.get("thread_id"),
            timezone=str(state.get("timezone") or TIMEZONE),
            session_id=session_id,
            template=state.get("generate_input_template"),
        )
        payload = build_graphql_payload(
            "GenerateProjectGroupRevisionFromChat",
            GENERATE_REVISION_MUTATION,
            {"input": input_body},
        )
        data = await self._request_graphql(page, payload, state["headers"])
        result = _extract_mutation_result(data)
        revision = result.get("projectGroupRevision")
        if not isinstance(revision, dict) or not revision.get("id"):
            raise RuntimeError("Anything 生成任务未返回 projectGroupRevision.id")
        self._update_thread_from_revision(session_id, revision)
        return str(revision["id"])

    async def _poll_revision(
        self,
        page: Page,
        session_id: str,
        revision_id: str,
        state: dict[str, Any],
    ) -> AsyncIterator[str]:
        """轮询 revision 并输出增量文本；输入为 revision_id，输出为 AsyncIterator[str]。"""
        interval = max(0.2, get_float("anything", "poll_interval_seconds", 2.0))
        timeout = max(interval, get_float("anything", "poll_timeout_seconds", 180.0))
        deadline = asyncio.get_running_loop().time() + timeout
        seen_text = ""
        last_status = ""
        while True:
            data = await self._request_graphql(
                page,
                build_graphql_payload(
                    "ProjectGroupRevisionForChatById",
                    POLL_REVISION_QUERY,
                    {"id": revision_id},
                ),
                state["headers"],
            )
            revision = _extract_revision_from_query(data)
            self._update_thread_from_revision(session_id, revision)
            text = extract_revision_text(revision)
            if text.startswith(seen_text) and len(text) > len(seen_text):
                yield text[len(seen_text):]
                seen_text = text
            elif text and text != seen_text:
                yield text
                seen_text = text

            last_status = _normalized_status(revision)
            if _is_failed_status(last_status):
                raise RuntimeError(f"Anything 生成失败，revision status={last_status}")
            if _is_success_status(last_status):
                if not seen_text:
                    raise RuntimeError("Anything 生成完成但 response 为空")
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"Anything 轮询超时，最后状态={last_status or 'unknown'}")
            await asyncio.sleep(interval)

    def _update_thread_from_revision(
        self,
        session_id: str,
        revision: dict[str, Any] | None,
    ) -> None:
        """从 revision 更新 thread_id；输入为 GraphQL revision，输出为本地 session state 变更。"""
        if not isinstance(revision, dict) or session_id not in self._session_state:
            return
        thread = revision.get("thread")
        if isinstance(thread, dict) and thread.get("id"):
            self._session_state[session_id]["thread_id"] = str(thread["id"])


def register_anything_plugin() -> None:
    """注册 Anything 插件到全局 Registry；由 core.app.lifespan 在服务启动时调用。"""
    PluginRegistry.register(AnythingPlugin())
