"""语音提交：有提问→作答；没提问→当作新消息发给助手（prompt.submit）。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_remote.state import PendingRequest, SessionRow  # noqa: E402

try:
    from test_hardening import FakeGateway, make_bridge
except ImportError:  # pragma: no cover
    from tests.test_hardening import FakeGateway, make_bridge


def _bridge(gw=None):
    b = make_bridge(gw or FakeGateway())
    b.state.connected_backend = True
    b.state.sessions = [SessionRow(id="s1", title="会话一")]
    return b


class VoiceSubmitTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_pending_goes_out_as_prompt(self) -> None:
        gw = FakeGateway()
        b = _bridge(gw)
        b.state.voice_preview = "把测试跑一遍"
        await b._submit_voice_text("把测试跑一遍")
        self.assertEqual([c[0] for c in gw.calls], ["prompt.submit"])
        self.assertEqual(gw.calls[0][1]["text"], "把测试跑一遍")
        self.assertEqual(gw.calls[0][1]["session_id"], "s1")
        self.assertIn("已发给助手", b.state.confirm_text)

    async def test_pending_clarify_is_answered_not_submitted(self) -> None:
        gw = FakeGateway(rpc_result={"status": "ok"})
        b = _bridge(gw)
        b.state.pending = PendingRequest(
            id="R1", method="clarify",
            params={"questions": [{"qid": "q1", "question": "哪个方案？", "choices": ["A", "B"]}]})
        await b._submit_voice_text("B")
        self.assertEqual(gw.calls, [], "有提问时不该再发新消息")
        self.assertEqual(gw.answered, [("R1", {"answers": {"q1": "B"}})])

    async def test_pending_approval_cannot_be_spoken(self) -> None:
        gw = FakeGateway()
        b = _bridge(gw)
        b.state.pending = PendingRequest(
            id="R2", method="approval",
            params={"tool_name": "terminal", "command": "rm -rf /tmp/x",
                    "choices": ["once", "deny"]})
        await b._submit_voice_text("同意")
        self.assertEqual(gw.calls, [])
        self.assertEqual(gw.answered, [], "审批不能靠嘴批")
        self.assertIn("审批只能用按键", b.state.confirm_detail)

    async def test_offline_does_not_submit(self) -> None:
        gw = FakeGateway()
        b = _bridge(gw)
        b.gateway.connected = False
        await b._submit_voice_text("在吗")
        self.assertEqual(gw.calls, [])
        self.assertIn("没连上", b.state.confirm_text)


if __name__ == "__main__":
    unittest.main()
