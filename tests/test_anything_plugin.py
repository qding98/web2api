"""
Anything 插件单元测试：验证 auth 编译、GraphQL payload/input 构建、revision 文本抽取与轮询增量输出。

本脚本在项目中的作用：
- 作为 tests 下的 unittest 测试文件由 python -m unittest 调用。
- 直接调用 core.plugin.anything 的纯函数，并用 mock 替代真实页面 fetch。
- 输入来源是测试内构造的 auth JSON、GraphQL mock 响应和用户消息。
- 输出内容是断言结果，不访问真实 Anything 网络。
- 对外不提供业务接口，仅作为回归测试入口。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from core.plugin.anything import (
    AnythingPlugin,
    build_anything_auth_settings,
    build_generate_input,
    build_graphql_payload,
    extract_revision_text,
)


class _FakePage:
    """测试用页面替身；仅提供插件轮询测试所需的 url 属性。"""

    url = "https://www.anything.com/"


class TestAnythingHelpers(unittest.TestCase):
    """Anything 纯函数测试集合，避免真实浏览器和真实网络依赖。"""

    def test_build_auth_settings_prefers_explicit_authorization(self) -> None:
        """验证 auth JSON 能解析 Authorization、localStorage key、cookie 与项目 ID。"""
        settings = build_anything_auth_settings(
            {
                "authorization": "Bearer abc",
                "refresh_token": "refresh-1",
                "authorizationStorageKey": "customToken",
                "projectGroupId": "pg-1",
                "threadId": "thread-1",
                "headers": {"x-test": "ok"},
            }
        )

        self.assertEqual(settings.authorization_header, "Bearer abc")
        self.assertEqual(settings.local_storage_value, "abc")
        self.assertEqual(settings.local_storage_keys[0], "customToken")
        self.assertEqual(settings.cookies[0]["name"], "refresh_token")
        self.assertEqual(settings.project_group_id, "pg-1")
        self.assertEqual(settings.thread_id, "thread-1")
        self.assertEqual(settings.extra_headers["x-test"], "ok")

    def test_build_generate_input_default_and_template(self) -> None:
        """验证默认 input 与模板 input 都能正确注入 message/project/thread。"""
        default_input = build_generate_input(
            message="hello",
            project_group_id="pg",
            thread_id="thread",
            timezone="Asia/Shanghai",
            session_id="session",
        )
        self.assertEqual(
            default_input,
            {"projectGroupId": "pg", "content": "hello", "threadId": "thread"},
        )

        templated = build_generate_input(
            message="hello",
            project_group_id="pg",
            thread_id=None,
            timezone="Asia/Shanghai",
            session_id="session",
            template={
                "projectGroupId": "{project_group_id}",
                "threadId": "{thread_id}",
                "content": "{message}",
                "meta": {"timezone": "{timezone}", "session": "{session_id}"},
            },
        )
        self.assertEqual(
            templated,
            {
                "projectGroupId": "pg",
                "content": "hello",
                "meta": {"timezone": "Asia/Shanghai", "session": "session"},
            },
        )

    def test_graphql_payload_and_revision_text(self) -> None:
        """验证 GraphQL payload 结构和 revision.response 的文本抽取逻辑。"""
        payload = build_graphql_payload("Op", "query Op { ok }", {"id": "1"})
        self.assertEqual(payload["operationName"], "Op")
        self.assertEqual(payload["variables"], {"id": "1"})
        self.assertEqual(extract_revision_text({"response": "done"}), "done")
        self.assertEqual(
            extract_revision_text({"response": {"text": "done"}}),
            json.dumps({"text": "done"}, ensure_ascii=False),
        )


class TestAnythingPlugin(unittest.IsolatedAsyncioTestCase):
    """Anything 插件异步流程测试集合，使用 mock GraphQL 响应验证增量输出。"""

    async def test_stream_completion_polls_revision_and_yields_delta(self) -> None:
        """验证 stream_completion 会先创建 revision，再轮询并输出 response 增量。"""
        plugin = AnythingPlugin()
        plugin._session_state["anything-test"] = {
            "project_group_id": "pg",
            "thread_id": "thread",
            "headers": {"Content-Type": "application/json"},
            "generate_input_template": None,
            "timezone": "Asia/Shanghai",
        }
        calls: list[str] = []

        async def fake_fetch(*_args: object, **kwargs: object) -> dict:
            """根据 GraphQL operationName 返回 mock 响应；输入来自插件 page.fetch 调用。"""
            body = json.loads(str(kwargs["body"]))
            calls.append(body["operationName"])
            if body["operationName"] == "GenerateProjectGroupRevisionFromChat":
                return {
                    "status": 200,
                    "json": {
                        "data": {
                            "generateProjectGroupRevisionFromChat": {
                                "success": True,
                                "projectGroupRevision": {
                                    "id": "rev-1",
                                    "response": "",
                                    "status": "RUNNING",
                                    "thread": {"id": "thread"},
                                },
                            }
                        }
                    },
                }
            if len(calls) == 2:
                response = "hel"
                status = "RUNNING"
            else:
                response = "hello"
                status = "COMPLETED"
            return {
                "status": 200,
                "json": {
                    "data": {
                        "projectGroupRevisionById": {
                            "id": "rev-1",
                            "response": response,
                            "status": status,
                            "thread": {"id": "thread"},
                        }
                    }
                },
            }

        with patch("core.plugin.anything.request_json_via_page_fetch", fake_fetch):
            chunks = [
                chunk
                async for chunk in plugin.stream_completion(
                    object(),
                    _FakePage(),
                    "anything-test",
                    "hello",
                )
            ]

        self.assertEqual(calls[0], "GenerateProjectGroupRevisionFromChat")
        self.assertEqual(calls[1:], ["ProjectGroupRevisionForChatById"] * 2)
        self.assertEqual(chunks, ["", "hel", "lo"])


if __name__ == "__main__":
    unittest.main()
