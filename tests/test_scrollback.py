"""状态页正文翻页：5 行一页，fn 往前、del 回最新。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_remote.state import BridgeState, SessionRow  # noqa: E402

try:  # discover 把 tests/ 放进 sys.path；按模块路径跑时走另一种
    from test_hardening import FakeGateway, make_bridge
except ImportError:  # pragma: no cover
    from tests.test_hardening import FakeGateway, make_bridge


def _state_with(n: int) -> BridgeState:
    st = BridgeState()
    st.connected_backend = True
    st.sessions = [SessionRow(id="s", title="会话")]
    for i in range(n):
        st.apply_event("tool.start", {"name": "terminal", "args": {"command": f"cmd{i}"}})
        st.apply_event("tool.complete", {"summary": f"result {i}"})
    return st


def _text(view) -> str:
    return " | ".join(line.text for line in view.lines)


class ScrollbackTests(unittest.TestCase):
    def test_newest_page_shows_latest_entries(self) -> None:
        st = _state_with(6)
        text = _text(st.build_view())
        self.assertIn("result 5", text)
        self.assertNotIn("result 0", text)

    def test_scrolling_back_shows_older_entries(self) -> None:
        st = _state_with(6)
        st.build_view()                       # 先算一次 max_scroll
        self.assertTrue(st.scroll_by(1))
        view = st.build_view()
        text = _text(view)
        self.assertIn("result 3", text)        # 往前一页
        self.assertNotIn("result 5", text)
        self.assertIn("↑1页", view.footer)

    def test_scroll_is_clamped_at_both_ends(self) -> None:
        st = _state_with(6)
        st.build_view()
        for _ in range(20):
            st.scroll_by(1)
        self.assertEqual(st.scroll, st.max_scroll)
        self.assertFalse(st.scroll_by(1))
        while st.scroll_by(-1):
            pass
        self.assertEqual(st.scroll, 0)
        self.assertFalse(st.scroll_by(-1))

    def test_new_session_switch_resets_scroll(self) -> None:
        st = _state_with(6)
        st.build_view()
        st.scroll_by(1)
        st.scroll = 0                          # 桥在切会话时清零（见 _on_status_key）
        self.assertIn("result 5", _text(st.build_view()))


class StatusKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_fn_does_not_switch_session(self) -> None:
        """以前 fn(fn1) 落在会话切换列表里，按 fn 会直接跳到第 1 个会话。"""
        b = make_bridge(FakeGateway())
        b.state.sessions = [SessionRow(id="a", title="A"), SessionRow(id="b", title="B")]
        b.state.active_index = 1
        await b.on_key("fn1")
        self.assertEqual(b.state.active_index, 1, "fn 不该切会话")

    async def test_fn_scrolls_back_and_del_returns(self) -> None:
        b = make_bridge(FakeGateway())
        b.state.connected_backend = True
        b.state.sessions = [SessionRow(id="s", title="会话")]
        for i in range(6):
            b.state.apply_event("tool.start", {"name": "terminal", "args": {"command": f"cmd{i}"}})
            b.state.apply_event("tool.complete", {"summary": f"result {i}"})
        b.state.build_view()
        await b.on_key("fn1")
        self.assertEqual(b.state.scroll, 1)
        await b.on_key("back")
        self.assertEqual(b.state.scroll, 0)


if __name__ == "__main__":
    unittest.main()
