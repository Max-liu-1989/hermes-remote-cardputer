"""加固回归测试：这 5 件事在交付版里是“能按下去但不对”。

每条都对应一个真实缺陷，改坏了就说明退回去了：
  1. 危险判定不看工具名 → python -c 一条命令的单次按键就能清空目录；
  2. 没有命令原文时反而“安全” → 屏上让你按 2，逻辑上按 1 就放行；
  3. 轮询重复入队 → 同一个请求在队列里堆 4 份，答完还会再答一遍；
  4. 断线重连后作答“假成功” → 后端根本没收到，屏上和审计都写已发送；
  5. /stt + voice.confirm 绕过“先预览再发”。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_remote import safety
from hermes_remote.bridge import Bridge, BridgeConfig
from hermes_remote.device_server import DeviceHub
from hermes_remote.gateway import GatewayError, RpcError
from hermes_remote.state import BridgeState, PendingRequest


class ToolAwareSafetyTests(unittest.TestCase):
    def test_inline_script_is_dangerous_when_tool_is_terminal(self) -> None:
        cmd = "python3 -c 'import shutil,os; shutil.rmtree(os.path.expanduser(\"~/Documents\"))'"
        self.assertTrue(safety.approval_is_dangerous(cmd, "terminal"), cmd)
        self.assertIn("内联脚本", safety.matched_dangers(cmd, "terminal"))

    def test_empty_command_is_dangerous(self) -> None:
        self.assertTrue(safety.approval_is_dangerous("", "terminal"))
        self.assertIn("没有命令原文", safety.matched_dangers("", "terminal"))

    def test_readonly_commands_stay_single_press(self) -> None:
        for cmd in ("ls -la", "pytest -q", "git status", "git diff --stat",
                    "cat README.md", "grep -rn bridge .", "python3 -m http.server"):
            self.assertFalse(safety.approval_is_dangerous(cmd, "terminal"), cmd)

    def test_structural_dangers(self) -> None:
        cases = {
            "cat a > b": "shell 组合/重定向",
            "echo hi && rm -rf /tmp/x": "shell 组合/重定向",
            "find . -name '*.pyc' -delete": "find 写操作",
            "git commit -m 'x'": "git 写操作",
            "git push origin main": "git 写操作",
            "mv ~/a ~/b": "写入类命令（mv）",
            "sed -i '' s/a/b/ f.txt": "写入类命令（sed）",
        }
        for cmd, label in cases.items():
            self.assertIn(label, safety.matched_dangers(cmd, "terminal"), cmd)

    def test_tool_absent_keeps_text_layer_only(self) -> None:
        self.assertFalse(safety.approval_is_dangerous("python3 -c 'print(1)'"))
        self.assertTrue(safety.approval_is_dangerous("rm -rf /tmp/x"))


class FakeGateway:
    """只实现桥用到的那几个方法；行为由测试决定。"""

    connected = True

    def __init__(self, *, open_requests=None, rpc_result=None, rpc_error=None,
                 send_error=None):
        self.last_seen = 0
        self._open = open_requests or []
        self.rpc_result = rpc_result
        self.rpc_error = rpc_error
        self.send_error = send_error
        self.answered: list[tuple[str, dict]] = []
        self.frames: list[tuple[str, dict]] = []
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None, timeout=None):
        self.calls.append((method, dict(params or {})))
        return self.rpc_result if self.rpc_result is not None else {"status": "streaming"}

    async def events_since(self, sid, last_seen):
        return {"open_requests": list(self._open)}

    async def answer_via_rpc(self, rid, result):
        self.answered.append((rid, result))
        if self.rpc_error is not None:
            raise self.rpc_error
        return self.rpc_result if self.rpc_result is not None else {"status": "ok"}

    async def send_response(self, rid, result=None, error=None):
        if self.send_error is not None:
            raise self.send_error
        self.frames.append((rid, result or {}))

    async def attach(self, sid):
        return {"session_id": sid, "status": "idle", "running": False, "info": {"title": "t"}}

    async def active_sessions(self, current: str = ""):
        return list(getattr(self, "active_list", []))


def make_bridge(gw, **cfg):
    from hermes_remote.audit import AuditLog
    import tempfile

    audit = AuditLog(Path(tempfile.mkdtemp(prefix="hr-audit-")) / "audit.jsonl")
    b = Bridge(BridgeConfig(backend_url="ws://127.0.0.1:1/api/ws", **cfg), gateway=gw, audit=audit)
    return b


def approval_req(rid="A", command="pytest -q"):
    return PendingRequest(id=rid, method="approval",
                          params={"tool_name": "terminal", "command": command})


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_reply_is_not_reported_as_sent(self) -> None:
        gw = FakeGateway(rpc_result={"status": "expired"})
        b = make_bridge(gw)
        b.state.pending = approval_req()
        await b._answer({"choice": "once"})
        self.assertIsNone(b.state.pending, "过期请求还挂在屏上")
        self.assertIn("过期", b.state.last_key_result)
        events = [json.loads(line)["event"] for line in
                  Path(b.audit.path).read_text(encoding="utf-8").splitlines()[-3:]]
        self.assertIn("request.expired", events)
        self.assertNotIn("request.answered", events)

    async def test_delivery_failure_keeps_the_card(self) -> None:
        gw = FakeGateway(rpc_error=GatewayError("gateway not connected"))
        b = make_bridge(gw)
        b.state.pending = approval_req()
        await b._answer({"choice": "once"})
        self.assertIsNotNone(b.state.pending, "没送到却把卡片关掉了，用户不会再重试")
        self.assertIn("没送到", b.state.last_key_result)

    async def test_old_backend_falls_back_to_response_frame(self) -> None:
        gw = FakeGateway(rpc_error=RpcError(-32601, "no such method request.answer"))
        b = make_bridge(gw)
        b.state.pending = approval_req()
        await b._answer({"choice": "once"})
        self.assertEqual(gw.frames, [("A", {"choice": "once"})])
        self.assertIsNone(b.state.pending)

    async def test_successful_rpc_is_preferred_over_frame(self) -> None:
        gw = FakeGateway(rpc_result={"status": "ok"})
        b = make_bridge(gw)
        b.state.pending = approval_req()
        await b._answer({"choice": "deny"})
        self.assertEqual(gw.answered, [("A", {"choice": "deny"})])
        self.assertEqual(gw.frames, [])


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_polling_does_not_duplicate_queue(self) -> None:
        open_reqs = [
            {"id": "A", "method": "approval", "params": {"tool_name": "terminal", "command": "ls"}},
            {"id": "B", "method": "clarify", "params": {"questions": [{"question": "?", "choices": ["y"]}]}},
        ]
        gw = FakeGateway(open_requests=open_reqs)
        b = make_bridge(gw)
        b.state.sessions = []
        from hermes_remote.state import SessionRow
        b.state.sessions = [SessionRow(id="s1", title="t")]
        for _ in range(4):
            await b._poll_events()
        self.assertEqual(b.state.pending.id, "A")
        self.assertEqual([q.id for q in b._queue], ["B"], "轮询把同一个请求重复塞进队列")

    async def test_answered_request_is_not_resurrected_by_replay(self) -> None:
        gw = FakeGateway(open_requests=[
            {"id": "A", "method": "approval", "params": {"tool_name": "terminal", "command": "ls"}},
        ])
        b = make_bridge(gw)
        from hermes_remote.state import SessionRow
        b.state.sessions = [SessionRow(id="s1", title="t")]
        b.state.pending = approval_req()
        await b._answer({"choice": "deny"})
        await b._poll_events()          # 后端还在 replay 同一条
        self.assertIsNone(b.state.pending, "答过的请求被 replay 复活了")

    async def test_cancel_event_purges_queued_copy(self) -> None:
        gw = FakeGateway()
        b = make_bridge(gw)
        b.state.pending = approval_req("A")
        b._queue.append(approval_req("B"))
        await b.on_event("request.cancel", {"id": "B", "session_id": "s1"})
        self.assertEqual(b._queue, [])


class ReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_from_snapshot_never_clears_card(self) -> None:
        """open_requests 不含 srq- 请求：缺项从来不能当"已处理"。"""
        gw = FakeGateway()
        b = make_bridge(gw)
        b.state.pending = approval_req("A")
        b._absorb_open_requests({"open_requests": []})
        for _ in range(5):
            b._reconcile_pending({"open_requests": []})
        self.assertIsNotNone(b.state.pending)

    async def test_cancel_event_clears_card(self) -> None:
        """真正撤卡的依据：后端发 cancel 事件（比如在 Mac 上答过了）。"""
        b = make_bridge(FakeGateway())
        b.state.pending = approval_req("A")
        b.state.apply_event("request.cancel", {"id": "A"})
        self.assertIsNone(b.state.pending)

    async def test_snapshot_keeps_live_request(self) -> None:
        b = make_bridge(FakeGateway())
        b.state.pending = approval_req("A")
        for _ in range(3):
            b._reconcile_pending({"open_requests": [{"id": "A"}]})
        self.assertIsNotNone(b.state.pending)


class VoiceStagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stt_confirm_goes_through_preview(self) -> None:
        gw = FakeGateway()
        b = make_bridge(gw)
        b.state.pending = PendingRequest(id="R1", method="clarify",
                                         params={"questions": [{"question": "选哪个?", "choices": ["甲", "乙"]}]})
        await b.on_key("voice:直接发出去")
        self.assertEqual(gw.answered, [], "voice: 前缀绕过了预览，直接提交了")
        self.assertEqual(b.state.voice_preview, "直接发出去")
        self.assertIsNotNone(b.state.pending)
        # 真正按 enter 才发
        await b.on_key("enter")
        self.assertEqual(gw.answered[0][0], "R1")


class RestoreTests(unittest.TestCase):
    def test_restore_does_not_clobber_current_card(self) -> None:
        st = BridgeState()
        st.pending = PendingRequest(id="live", method="approval", params={"command": "ls"})
        st.restore_requests([{"id": "old", "method": "approval", "params": {}}])
        self.assertEqual(st.pending.id, "live")


class AttachTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_live_session_id_from_active_list(self) -> None:
        """"桌面 App 的后端只让 attach 活动列表里的短 id（432cd0bc），
        不认 session.list 的数据库 id（20261018_122435_7a2cca）。"""

        class DevGW(FakeGateway):
            def __init__(self):
                super().__init__()
                self.attached: list[str] = []

            async def active_sessions(self, current=""):
                return [{"id": "432cd0bc", "session_key": "20261018_122435_7a2cca",
                         "title": "真会话", "status": "working", "last_active": 1.0}]

            async def list_sessions(self, limit=8):
                return [{"id": "20261018_122435_7a2cca", "title": "数据库行"}]

            async def attach(self, sid):
                self.attached.append(sid)
                return {"session_id": sid, "status": "working", "running": True,
                        "info": {"title": "真会话"}}

        gw = DevGW()
        b = make_bridge(gw)
        await b._attach_default_session()
        self.assertEqual(gw.attached, ["432cd0bc"])
        self.assertEqual(b.state.active.id, "432cd0bc")
        await b.stop()


class AuditPathTests(unittest.TestCase):
    def test_default_audit_path_is_expanded(self) -> None:
        from hermes_remote.audit import AuditLog

        path = AuditLog().path
        self.assertNotIn("~", str(path), "默认审计路径没展开，会在当前目录里建一个字面量 ~/ 目录")
        self.assertEqual(str(path), str(Path.home() / ".hermes-remote" / "audit.jsonl"))


class StubWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send_str(self, text):
        self.sent.append(text)


class DeviceEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_voice_token_is_rejected(self) -> None:
        seen: list[str] = []

        async def on_key(k):
            seen.append(k)

        hub = DeviceHub("t" * 32, on_key=on_key, on_voice_upload=None)
        hub.voice_tokens = {"tok": ("旧话", time.time() - 300)}
        ws = StubWS()
        await hub._handle_text(json.dumps({"t": "voice.confirm", "token": "tok"}), ws)
        self.assertEqual(seen, [])
        self.assertTrue(any("stale" in s for s in ws.sent))

    async def test_voice_token_confirm_is_staged(self) -> None:
        seen: list[str] = []

        async def on_key(k):
            seen.append(k)

        hub = DeviceHub("t" * 32, on_key=on_key, on_voice_upload=None)
        hub.voice_tokens = {"tok": ("新话", time.time())}
        await hub._handle_text(json.dumps({"t": "voice.confirm", "token": "tok"}), StubWS())
        self.assertEqual(seen, ["voice:新话"])


if __name__ == "__main__":
    unittest.main()


class PendingCardLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """卡片被误杀的现场：快照不列服务端请求，桥却用快照对账。"""

    def _req(self):
        from hermes_remote.state import PendingRequest
        return PendingRequest(id="R1", method="clarify",
                              params={"questions": [{"qid": "q1", "question": "?",
                                                     "choices": ["a", "b"]}]})

    async def test_attach_snapshot_missing_does_not_drop_card(self):
        b = make_bridge(FakeGateway())
        b.state.pending = self._req()
        b._absorb_open_requests({"open_requests": []})   # 快照里没有它
        self.assertIsNotNone(b.state.pending, "快照缺项不该撤卡")

    async def test_poll_missing_does_not_drop_card(self):
        """open_requests 不含 srq- 请求：缺项不能当"已处理"，否则卡片一闪而过。"""
        b = make_bridge(FakeGateway())
        b.state.pending = self._req()
        b._reconcile_pending({"open_requests": []})
        b._reconcile_pending({"open_requests": []})
        self.assertIsNotNone(b.state.pending, "轮询缺项不该撤卡")
        self.assertFalse(b._is_answered("R1"))
