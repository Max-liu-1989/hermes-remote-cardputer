"""第二轮加固：按《远控 Agent 整改建议》第一、二优先级做的审批模型与通信鉴权。

每条都对应整改建议里的一条硬规则：
  * 命令缺失/为空/类型错误/屏上无法完整查阅 → 设备端**禁止批准**（不是“危险所以要按两次”）；
  * 屏上显示与按键处理读**同一份** ApprovalFacts，不再各自解释 params；
  * 长命令要**翻完所有页**才允许批；
  * 二次确认绑定到具体请求内容（digest），请求切换/内容变化/断线重连后旧确认失效；
  * 后端说 expired 时不得出现“已发送”；
  * 设备 token 不再放 URL，改连上后第一条 auth 消息；无鉴权连接不推帧。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import WSMsgType

from hermes_remote.bridge import Bridge, BridgeConfig
from hermes_remote.device_server import DeviceHub
from hermes_remote.safety import approval_facts, approval_is_dangerous, wrap_command
from hermes_remote.state import BridgeState, PendingRequest

try:  # discover 把 tests/ 放进 sys.path；按模块路径跑时走另一种
    from test_hardening import FakeGateway, make_bridge
except ImportError:  # pragma: no cover
    from tests.test_hardening import FakeGateway, make_bridge


def approval_req(command=None, *, tool="terminal", rid="A", choices=("once", "deny"), **extra):
    params = {"tool_name": tool, "choices": list(choices), **extra}
    if command is not None:
        params["command"] = command
    return PendingRequest(id=rid, method="approval", params=params)


class FactsTests(unittest.TestCase):
    def test_missing_command_is_invalid_not_merely_dangerous(self) -> None:
        facts = approval_facts({"tool_name": "terminal", "description": "写一个文件"})
        self.assertFalse(facts.valid)
        self.assertEqual(facts.problem, "没有命令原文")
        self.assertEqual(facts.command, "", "description 不许顶替 command 当作命令")
        self.assertEqual(facts.context, "写一个文件")

    def test_empty_and_wrong_type_are_invalid(self) -> None:
        for bad in ("", "   ", 42, {"a": 1}, ["rm", "-rf"]):
            facts = approval_facts({"tool_name": "terminal", "command": bad})
            self.assertFalse(facts.valid, bad)

    def test_description_does_not_change_danger_verdict(self) -> None:
        # 描述里写着 rm -rf，但命令原文缺失 → 仍然无效（不能靠描述骗过判定）
        facts = approval_facts({"tool_name": "terminal", "description": "rm -rf /"})
        self.assertFalse(facts.valid)
        self.assertIn("没有命令原文", facts.reasons)

    def test_two_page_command_is_valid_and_paged(self) -> None:
        cmd = "python3 -c 'import shutil, os; shutil.rmtree(os.path.expanduser(\"~/.cache\"))'"
        facts = approval_facts({"tool_name": "terminal", "command": cmd})
        self.assertTrue(facts.valid)
        self.assertIn("内联脚本", facts.reasons)

    def test_overlong_command_is_not_approvable_on_device(self) -> None:
        facts = approval_facts({"tool_name": "terminal", "command": "echo " + "a " * 400})
        self.assertFalse(facts.valid)
        self.assertIn("过长", facts.problem)

    def test_wrap_keeps_every_character(self) -> None:
        cmd = " ".join(f"arg{i}" for i in range(40))
        joined = "".join("".join(page) for page in wrap_command(cmd)).replace(" ", "")
        self.assertEqual(joined, cmd.replace(" ", ""))


class ApprovalViewTests(unittest.TestCase):
    def test_view_and_key_use_the_same_verdict(self) -> None:
        st = BridgeState()
        st.pending = approval_req("python3 -c 'import shutil; shutil.rmtree(\"/tmp/x\")'")
        view = st.build_view()
        self.assertIn("危险操作", view.lines[0].text)
        self.assertTrue(st.pending.approval.dangerous)

    def test_missing_command_screen_says_go_to_mac(self) -> None:
        st = BridgeState()
        st.pending = approval_req(None)
        view = st.build_view()
        self.assertIn("不能在设备上批", view.lines[0].text)
        self.assertIn("没有命令原文", view.lines[1].text)

    def test_long_command_page_indicator(self) -> None:
        st = BridgeState()
        st.pending = approval_req("python3 -c '" + "x" * 200 + "'")
        view = st.build_view()
        self.assertGreater(st.pending.approval.page_count, 1)
        self.assertIn("翻完才能批", view.lines[-1].text)
        self.assertIn("[fn]", view.footer)


class ApprovalFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_deny_shows_confirmation_on_screen(self) -> None:
        """按完必须立刻看到“已拒绝”：真机上按 2 没有任何反馈，人不知道成没成。"""
        gw = FakeGateway()
        b = make_bridge(gw)
        b.state.pending = approval_req("rm -rf /tmp/x")
        await b.on_key("2")
        self.assertEqual(gw.answered, [("A", {"choice": "deny"})])
        self.assertIsNone(b.state.pending)
        self.assertIn("已拒绝", b.state.confirm_text)
        view = b.state.build_view()
        self.assertEqual(view.page, "confirm")
        self.assertTrue(any("已拒绝" in ln.text for ln in view.lines))

    async def test_invalid_command_can_only_be_denied(self) -> None:
        gw = FakeGateway()
        b = make_bridge(gw)
        b.state.pending = approval_req(None)
        await b.on_key("1")
        self.assertEqual(gw.answered, [], "缺命令原文却放行了")
        self.assertIsNotNone(b.state.pending)
        await b.on_key("2")
        self.assertEqual(gw.answered, [("A", {"choice": "deny"})])

    async def test_multi_page_command_requires_reading_all_pages(self) -> None:
        gw = FakeGateway()
        b = make_bridge(gw)
        cmd = "python3 -c '" + "y" * 120 + "'"   # 约 4 页
        req = approval_req(cmd)
        b.state.pending = req
        pages = b.state.pending.approval.page_count
        self.assertGreater(pages, 2)

        await b.on_key("2")                      # 拒绝永远可用
        self.assertEqual(gw.answered, [("A", {"choice": "deny"})])

        gw2 = FakeGateway()
        b2 = make_bridge(gw2)
        req2 = approval_req(cmd)
        b2.state.pending = req2
        await b2.on_key("1")                     # 还没翻页
        self.assertEqual(gw2.answered, [], "没翻完就允许批了")
        for _ in range(pages + 1):
            await b2.on_key("fn1")
        # 翻完之后是危险命令，还得按两次
        await b2.on_key("1")
        self.assertEqual(gw2.answered, [])
        await b2.on_key("1")
        self.assertEqual(gw2.answered[0][1], {"choice": "once"})

    async def test_arm_is_bound_to_request_content(self) -> None:
        gw = FakeGateway()
        b = make_bridge(gw)
        req = approval_req("rm -rf /tmp/hr-arm")
        b.state.pending = req
        await b.on_key("1")                        # 上膛
        self.assertTrue(req.confirm_armed)
        req.params = {**req.params, "command": "rm -rf /tmp/hr-arm-other"}  # 内容变了
        await b.on_key("1")
        self.assertEqual(gw.answered, [], "内容变了旧确认还生效")
        await b.on_key("1")
        self.assertEqual(gw.answered[0][1], {"choice": "once"})

    async def test_disconnect_clears_arming(self) -> None:
        b = make_bridge(FakeGateway())
        req = approval_req("rm -rf /tmp/x")
        b.state.pending = req
        await b.on_key("1")
        self.assertTrue(req.confirm_armed)
        await b.on_backend_state(False)
        self.assertFalse(req.confirm_armed, "断线重连后旧确认仍生效")


class AuthOverWebSocketTests(unittest.IsolatedAsyncioTestCase):
    TOKEN = "a" * 32

    async def asyncSetUp(self) -> None:
        self.hub = DeviceHub(self.TOKEN, on_key=self._on_key, on_voice_upload=None)
        # 0.6s 在机器负载高时（测试进程被抢占）会误判成"没鉴权"：放宽到 2s，
        # 行为不变（另一个用例仍然验证"不鉴权就关连接"）。
        self.hub.auth_timeout = 2.0
        self.port = await self.hub.start(host="127.0.0.1", port=0)

    async def asyncTearDown(self) -> None:
        await self.hub.stop()

    async def _on_key(self, _key: str) -> None:
        return None

    def _url(self, query: str = "") -> str:
        return f"ws://127.0.0.1:{self.port}/ws{query}"

    async def test_connection_without_auth_is_closed_without_frames(self) -> None:
        from aiohttp import ClientSession

        async with ClientSession() as s:
            ws = await s.ws_connect(self._url())
            msg = await asyncio.wait_for(ws.receive(), timeout=3)
            self.assertEqual(msg.type, WSMsgType.CLOSE)
            self.assertEqual(msg.data, 4401)
            self.assertEqual(self.hub.device_count, 0, "未鉴权连接被当成设备在线")

    async def test_auth_message_is_accepted_and_frames_flow(self) -> None:
        from aiohttp import ClientSession

        self.hub.last_frame = b"frame-bytes"
        async with ClientSession() as s:
            ws = await s.ws_connect(self._url())
            await ws.send_str(json.dumps({"t": "auth", "token": self.TOKEN}))
            welcome = await asyncio.wait_for(ws.receive(), timeout=3)
            self.assertEqual(welcome.type, WSMsgType.TEXT)
            self.assertIn("welcome", welcome.data)
            msg = await asyncio.wait_for(ws.receive(), timeout=3)
            self.assertEqual(msg.type, WSMsgType.BINARY)
            self.assertEqual(msg.data, b"frame-bytes")
            self.assertEqual(self.hub.device_count, 1)

    async def test_wrong_auth_message_is_rejected(self) -> None:
        from aiohttp import ClientSession

        async with ClientSession() as s:
            ws = await s.ws_connect(self._url())
            await ws.send_str(json.dumps({"t": "auth", "token": "b" * 32}))
            msg = await asyncio.wait_for(ws.receive(), timeout=3)
            self.assertEqual(msg.type, WSMsgType.CLOSE)
            self.assertEqual(msg.data, 4401)

    async def test_query_token_still_works_for_old_firmware(self) -> None:
        from aiohttp import ClientSession

        async with ClientSession() as s:
            ws = await s.ws_connect(self._url(f"?token={self.TOKEN}"))
            await ws.send_str(json.dumps({"t": "hello", "fw": "old"}))
            msg = await asyncio.wait_for(ws.receive(), timeout=3)
            self.assertEqual(msg.type, WSMsgType.TEXT)
            self.assertIn("welcome", msg.data)

    async def test_healthz_hides_details_without_token(self) -> None:
        from aiohttp import ClientSession

        async with ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{self.port}/healthz") as r:
                body = await r.json()
                self.assertEqual(body, {"ok": True})
            async with s.get(f"http://127.0.0.1:{self.port}/healthz?token={self.TOKEN}") as r:
                body = await r.json()
                self.assertIn("devices", body)


class AuditNoCredentialTests(unittest.TestCase):
    def test_audit_lines_never_contain_token(self) -> None:
        import tempfile

        from hermes_remote.audit import AuditLog

        path = Path(tempfile.mkdtemp(prefix="hr-audit-")) / "a.jsonl"
        log = AuditLog(path)
        log.write("request.answered", request_id="srq-1", choice="once")
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("token", text.lower())
        self.assertIn("request.answered", text)


class ApprovalKeyOrderTests(unittest.TestCase):
    """真机上按 2 想拒绝，结果按成了「本会话都同意」——键位必须固定，不随后端顺序。"""

    def test_deny_is_always_key_2_even_in_backend_order(self) -> None:
        from hermes_remote.safety import approval_key_map

        backend_order = ("once", "session", "always", "deny")
        keys = approval_key_map(backend_order)
        self.assertEqual(keys["1"], "once")
        self.assertEqual(keys["2"], "deny")     # 不是 session
        self.assertEqual(keys["3"], "session")
        self.assertEqual(keys["4"], "always")

    def test_deny_offered_even_if_backend_omits_it(self) -> None:
        from hermes_remote.safety import approval_key_map

        keys = approval_key_map(("once",))
        self.assertEqual(keys, {"1": "once", "2": "deny"})

    def test_view_numbers_deny_as_two(self) -> None:
        from hermes_remote.state import BridgeState

        state = BridgeState()
        state.pending = approval_req("rm -rf /tmp/x",
                                    choices=("once", "session", "always", "deny"))
        view = state.build_view(now=100.0)
        footer = view.footer
        self.assertIn("[1]同意", footer)
        self.assertIn("[2]拒绝", footer)
        self.assertNotIn("[2]会话", footer)


if __name__ == "__main__":
    unittest.main()
