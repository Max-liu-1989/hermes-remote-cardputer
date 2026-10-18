"""助手回复要在板子上看得见：单独成页、可翻页。"""

from __future__ import annotations

import os
import time
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_remote.state import SessionRow  # noqa: E402

try:
    from test_hardening import FakeGateway, make_bridge
except ImportError:  # pragma: no cover
    from tests.test_hardening import FakeGateway, make_bridge


LONG = ("三件事都做完了。第一，转录库装上并且加了热词；第二，语音现在可以直接当新消息发；"
        "第三，状态页改成五行的紧凑排版，配上工具和结果成对滚动，还能用 fn 翻页。"
        "接下来要不要我把这几条写进知识库，再把板子上的按钮说明补一页？")


class ReplyViewTests(unittest.IsolatedAsyncioTestCase):
    async def _bridge_with_reply(self, text: str = LONG):
        b = make_bridge(FakeGateway())
        b.state.connected_backend = True
        b.state.set_reply(text, b._reply_pages(text))
        return b

    async def test_reply_becomes_its_own_page(self) -> None:
        b = await self._bridge_with_reply()
        view = b.state.build_view()
        self.assertEqual(view.page, "reply")
        self.assertIn("Hermes 回复", view.title)
        self.assertTrue(any("转录库" in line.text for line in view.lines))

    async def test_long_reply_is_paginated(self) -> None:
        b = await self._bridge_with_reply()
        view = b.state.build_view()
        self.assertGreater(len(b.state.reply_pages), 1, "长回复应该分页")
        self.assertIn("1/", view.footer)
        await b.on_key("fn1")
        self.assertEqual(b.state.reply_page, 1)
        self.assertIn("2/", b.state.build_view().footer)

    async def test_battery_and_rssi_reach_footer(self) -> None:
        """电量/信号来自板子心跳；以前桥从来没写进 state，底栏永远是空的。"""
        b = make_bridge(FakeGateway())
        b.state.connected_backend = True
        b.state.sessions = [SessionRow(id="s", title="会话")]
        b.hub.last_hello = {"batt": 95, "rssi": -66}
        await b.render_now(force=True)
        self.assertIn("电95%", b.state.build_view().footer)
        self.assertIn("-66dBm", b.state.build_view().footer)

    async def test_enter_dismisses_reply(self) -> None:
        b = await self._bridge_with_reply()
        self.assertEqual(b.state.build_view().page, "reply")
        await b.on_key("enter")
        self.assertEqual(b.state.build_view().page, "status")

    async def test_enter_reopens_dismissed_reply(self) -> None:
        """收起之后还能回去看——否则长回复被关掉就再也不见了。"""
        b = await self._bridge_with_reply()
        await b.on_key("enter")                     # 收起
        self.assertEqual(b.state.build_view().page, "status")
        await b.on_key("enter")                     # 再按一次：重新打开
        self.assertEqual(b.state.build_view().page, "reply")
        self.assertEqual(b.state.reply_page, 0)

    async def test_session_switch_forgets_reply(self) -> None:
        gw = FakeGateway()
        b = make_bridge(gw)
        b.state.connected_backend = True
        b.state.sessions = [SessionRow(id="a", title="A"), SessionRow(id="b", title="B")]
        text = "上一条回复"
        b.state.set_reply(text, b._reply_pages(text))
        await b.on_key("2")                          # 切到第二个会话
        self.assertEqual(b.state.reply_text, "")
        self.assertEqual(b.state.build_view().page, "status")

    async def test_event_message_complete_sets_reply(self) -> None:
        b = make_bridge(FakeGateway())
        b.state.connected_backend = True
        sent: list[dict] = []

        async def capture(payload):
            sent.append(payload)

        b.hub.broadcast_text = capture          # type: ignore[assignment]
        await b.on_event("message.complete", {"text": "好了，改完了。", "session_id": "s1"})
        self.assertTrue(b.state.reply_text.startswith("好了"))
        self.assertEqual(b.state.build_view().page, "reply")
        # 回复到了要响一声（和"有新请求"的音区分开）
        self.assertTrue(any(p.get("kind") == "reply" for p in sent), sent)


    async def test_no_beep_when_user_recently_pressed(self) -> None:
        """闲置门槛：5 分钟内按过键，回复就不再叼一声。"""
        b = make_bridge(FakeGateway())
        b.state.connected_backend = True
        sent: list[dict] = []

        async def capture(payload):
            sent.append(payload)

        b.hub.broadcast_text = capture          # type: ignore[assignment]
        b.state.last_key_at = time.time()       # 刚按过
        await b.on_event("message.complete", {"text": "好了", "session_id": "s1"})
        self.assertFalse(any(p.get("kind") == "reply" for p in sent), sent)

        sent.clear()
        b.state.last_key_at = time.time() - 301  # 闲置 5 分钟以上
        await b.on_event("message.complete", {"text": "又好了", "session_id": "s1"})
        self.assertTrue(any(p.get("kind") == "reply" for p in sent), sent)


if __name__ == "__main__":
    unittest.main()


class ReplyQueueTests(unittest.IsolatedAsyncioTestCase):
    """一轮里助手连发多条：前几条不能被覆盖，space 要能翻回去。"""

    async def test_second_reply_is_kept_and_cyclable(self) -> None:
        b = make_bridge(FakeGateway())
        b.state.set_reply("第一条回复", [["第一条回复"]])
        b.state.set_reply("第二条回复", [["第二条回复"]])
        self.assertEqual(len(b.state.reply_q), 2)
        self.assertEqual(b.state.reply_text, "第二条回复")
        self.assertTrue(b.state.cycle_reply(1))
        self.assertEqual(b.state.reply_text, "第一条回复")
        self.assertIn("1/2", b.state.build_view().footer)

    async def test_duplicate_reply_not_queued_twice(self) -> None:
        b = make_bridge(FakeGateway())
        b.state.set_reply("同一句", [["同一句"]])
        b.state.set_reply("同一句", [["同一句"]])
        self.assertEqual(len(b.state.reply_q), 1)

    async def test_space_switches_pending_when_queue_has_more(self) -> None:
        b = make_bridge(FakeGateway())
        from hermes_remote.state import PendingRequest

        def approval_req(rid: str):
            return PendingRequest(id=rid, method="approval",
                                  params={"tool_name": "terminal", "command": "pytest -q"})

        first = approval_req("A")
        second = approval_req("B")
        b.state.pending = first
        b._queue = [second]
        await b.on_key("space")
        self.assertEqual(b.state.pending.id, "B")
        self.assertEqual([q.id for q in b._queue], ["A"], "换过的那张要排到队尾，不能丢")
