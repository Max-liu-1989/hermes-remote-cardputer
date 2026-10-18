"""会话跟随：桌面端新开对话时，板子要跟着换（不能停在上一个会话）。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # discover 把 tests/ 放进 sys.path；按模块路径跑时走另一种
    from test_hardening import FakeGateway, make_bridge
except ImportError:  # pragma: no cover
    from tests.test_hardening import FakeGateway, make_bridge


class FollowSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_follows_newest_active_session(self) -> None:
        gw = FakeGateway()
        gw.active_list = [
            {"id": "aaaa", "title": "旧对话", "last_active": 100.0, "status": "idle"},
            {"id": "bbbb", "title": "刚开的新对话", "last_active": 900.0, "status": "working"},
        ]
        b = make_bridge(gw)
        b._attached_id = "aaaa"
        b.state.set_sessions([])
        await b._follow_active_session()
        self.assertEqual(b._attached_id, "bbbb", "没跟到桌面端最新的会话")
        self.assertIsNotNone(b.state.active)
        self.assertEqual(b.state.active.id, "bbbb")

    async def test_does_not_steal_after_manual_pick(self) -> None:
        gw = FakeGateway()
        gw.active_list = [
            {"id": "aaaa", "title": "桌面在用的", "last_active": 900.0},
            {"id": "bbbb", "title": "板子上手选的", "last_active": 100.0},
        ]
        b = make_bridge(gw)
        b._attached_id = "bbbb"
        b._manual_until = 10 ** 12     # 用户刚在板子上按了 1-3
        await b._follow_active_session()
        self.assertEqual(b._attached_id, "bbbb", "手动选的会话被自动跟随抢走了")

    async def test_prefers_running_session_over_newer_idle(self) -> None:
        gw = FakeGateway()
        gw.active_list = [
            {"id": "idle1", "title": "刚碰过但闲着", "last_active": 900.0, "status": "idle"},
            {"id": "run2", "title": "正在跑", "last_active": 100.0, "status": "working"},
        ]
        b = make_bridge(gw)
        b._attached_id = "idle1"
        await b._follow_active_session()
        self.assertEqual(b._attached_id, "run2", "有会话在跑就该跟它，而不是跟刚被碰过的空闲会话")

    async def test_stays_put_when_nothing_running(self) -> None:
        """没有任何会话在跑时不乱跳：attach 自己会刷新 last_active，跟风会自我强化。"""
        gw = FakeGateway()
        gw.active_list = [
            {"id": "other", "title": "另一个", "last_active": 900.0, "status": "idle"},
            {"id": "mine", "title": "我们的", "last_active": 100.0, "status": "idle"},
        ]
        b = make_bridge(gw)
        b._attached_id = "mine"
        await b._follow_active_session()
        self.assertEqual(b._attached_id, "mine")

    async def test_follows_when_our_session_disappeared(self) -> None:
        gw = FakeGateway()
        gw.active_list = [{"id": "other", "title": "只剩它", "last_active": 50.0, "status": "idle"}]
        b = make_bridge(gw)
        b._attached_id = "gone"
        await b._follow_active_session()
        self.assertEqual(b._attached_id, "other")

    async def test_no_change_no_attach(self) -> None:
        gw = FakeGateway()
        gw.active_list = [{"id": "aaaa", "title": "同一个", "last_active": 900.0}]
        b = make_bridge(gw)
        b._attached_id = "aaaa"
        calls = []
        orig = gw.attach

        async def counting(sid):
            calls.append(sid)
            return await orig(sid)

        gw.attach = counting  # type: ignore[assignment]
        await b._follow_active_session()
        self.assertEqual(calls, [], "会话没变却又 attach 了一次")


class BotSessionFilterTests(unittest.IsolatedAsyncioTestCase):
    """群聊/bot 会话一直 working，会把板子劫持走、挤掉自己的对话。"""

    def _bot_row(self) -> dict:
        return {"id": "bot1", "title": "Plan SATHOT upgrade as project manager",
                "preview": '[Group chat: "Hermes, project scheduling"] You are @project-scheduling',
                "status": "working", "last_active": 900.0}

    def test_group_chat_row_is_detected(self) -> None:
        from hermes_remote.state import _is_bot_session

        self.assertTrue(_is_bot_session(self._bot_row()))
        self.assertFalse(_is_bot_session(
            {"id": "x", "title": "远程审批终端加固", "preview": "已生效…", "status": "working"}))

    async def test_does_not_follow_bot_session(self) -> None:
        gw = FakeGateway()
        gw.active_list = [self._bot_row(),
                          {"id": "mine", "title": "我的对话", "preview": "…", "status": "idle"}]
        b = make_bridge(gw)
        b._attached_id = "mine"
        await b._follow_active_session()
        self.assertEqual(b._attached_id, "mine", "被 bot 会话劫持了")

    async def test_bot_rows_are_not_listed_and_ours_is_first(self) -> None:
        gw = FakeGateway()
        gw.active_list = [self._bot_row(),
                          {"id": "other", "title": "另一个", "preview": "…", "status": "idle"},
                          {"id": "mine", "title": "我的对话", "preview": "…", "status": "idle"}]
        b = make_bridge(gw)
        b._attached_id = "mine"
        rows = b._session_rows(gw.active_list)
        self.assertEqual([r.id for r in rows], ["mine", "other"])


if __name__ == "__main__":
    unittest.main()
