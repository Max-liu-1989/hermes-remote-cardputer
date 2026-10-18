"""协议与渲染的单元测试（stdlib unittest，不需要 pytest）。

跑：``/opt/hermes/.venv/bin/python -m unittest discover -s tests -v``
（或任何装了 pillow/websockets/aiohttp 的 Python）
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_remote import protocol, renderer  # noqa: E402
from hermes_remote.safety import (approval_command_lines, approval_is_dangerous,  # noqa: E402
                                  matched_dangers)
from hermes_remote.state import BridgeState, PendingRequest, SessionRow  # noqa: E402


class ProtocolTests(unittest.TestCase):
    def test_header_is_nine_bytes(self) -> None:
        self.assertEqual(protocol.HEADER_LEN, 9)

    def test_frame_roundtrip_rle(self) -> None:
        bitmap = bytes([0x00] * 100 + [0xFF] * 100 + [0xAA, 0x55] * 50)
        bitmap = (bitmap * 20)[:protocol.RAW_LEN]
        frame = protocol.build_frame(bitmap, seq=7)
        wire = frame.encode()
        self.assertEqual(wire[0], protocol.TYPE_SCREEN)
        parsed = protocol.ScreenFrame.decode(wire)
        self.assertEqual(parsed.seq, 7)
        self.assertEqual((parsed.width, parsed.height), (240, 135))
        self.assertEqual(protocol.decode_frame_bitmap(parsed), bitmap)

    def test_frame_roundtrip_raw(self) -> None:
        bitmap = bytes(range(256)) * 16
        bitmap = bitmap[:protocol.RAW_LEN]
        frame = protocol.build_frame(bitmap, seq=1, encoding=protocol.ENC_RAW)
        self.assertEqual(protocol.decode_frame_bitmap(protocol.ScreenFrame.decode(frame.encode())),
                         bitmap)

    def test_rle_of_alternating_bytes_is_bounded(self) -> None:
        bitmap = (b"\x55\xAA" * (protocol.RAW_LEN // 2))
        payload = protocol.rle_encode(bitmap)
        self.assertEqual(protocol.rle_decode(payload, protocol.RAW_LEN), bitmap)
        self.assertLessEqual(len(payload), 2 * protocol.RAW_LEN)

    def test_rle_long_run_splits_at_255(self) -> None:
        payload = protocol.rle_encode(b"\x00" * 600)
        decoded = protocol.rle_decode(payload, 600)
        self.assertEqual(decoded, b"\x00" * 600)
        self.assertGreater(len(payload), 2)

    def test_rle_rejects_underrun_and_overrun(self) -> None:
        with self.assertRaises(protocol.FrameError):
            protocol.rle_decode(bytes([10, 0]), 20)
        with self.assertRaises(protocol.FrameError):
            protocol.rle_decode(bytes([30, 0]), 20)
        with self.assertRaises(protocol.FrameError):
            protocol.rle_decode(bytes([0, 0]), 20)

    def test_unknown_type_rejected(self) -> None:
        wire = bytearray(protocol.build_frame(bytes(protocol.RAW_LEN), 1).encode())
        wire[0] = 0x09
        with self.assertRaises(protocol.FrameError):
            protocol.ScreenFrame.decode(bytes(wire))

    def test_truncated_frame_rejected(self) -> None:
        wire = protocol.build_frame(bytes(protocol.RAW_LEN), 1).encode()
        with self.assertRaises(protocol.FrameError):
            protocol.ScreenFrame.decode(wire[:-10])

    def test_parse_device_frames(self) -> None:
        self.assertEqual(protocol.parse_device_frame('{"t":"key","k":"1"}')["k"], "1")
        with self.assertRaises(ValueError):
            protocol.parse_device_frame('{"t":"key","k":"nope"}')
        with self.assertRaises(ValueError):
            protocol.parse_device_frame("not json")
        with self.assertRaises(ValueError):
            protocol.parse_device_frame('{"t":"weird"}')


class RendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fonts = renderer.Fonts()

    def _ink(self, img) -> int:
        return img.convert("L").histogram()[0]

    def test_font_has_cjk(self) -> None:
        self.assertIsNotNone(renderer.find_font_path(), "没有找到中文字体")
        img = renderer.render(_view(text="中文测试"), self.fonts)
        self.assertGreater(self._ink(img), 100, "中文没画出来（字体或编码问题）")

    def test_bitmap_is_exact_size(self) -> None:
        img = renderer.render(_view(text="hello"), self.fonts)
        packed = renderer.palette_bytes(img)
        self.assertEqual(len(packed), protocol.RAW_LEN4)

    def test_typical_frame_under_budget(self) -> None:
        st = BridgeState()
        st.connected_backend = True
        st.sessions = [SessionRow(id="s", title="改 tui_gateway/server.py", status="working",
                                  running=True, turn_started_at=1000.0)]
        st.status_note = "edit tui_gateway/server.py"
        st.last_output = "tests 42 passed, 0 failed"
        frame = renderer.frame_for(st.build_view(now=1252.0), 3, self.fonts)
        self.assertEqual(frame.encoding, protocol.ENC_RLE4)
        self.assertLess(len(frame.payload), 8000, "4 位调色板一帧 5–7KB（16 色 + 17px 字）")
        self.assertLess(len(frame.encode()), 9000)

    def test_long_text_is_truncated_inside_canvas(self) -> None:
        long_text = "这是一段特别长的问题" * 12
        lines = renderer.wrap_text(long_text, self.fonts.body, renderer.W - 12, 2)
        self.assertLessEqual(len(lines), 2)
        for line in lines:
            self.assertLessEqual(renderer._text_width(self.fonts.body, line), renderer.W - 12 + 1)
        self.assertTrue(lines[-1].endswith("…"))

    def test_inverted_bar_differs_per_state(self) -> None:
        status = renderer.render(_view(bar="normal"), self.fonts)
        alert = renderer.render(_view(bar="alert"), self.fonts)
        # normal 白底、alert 橙底（调色板里不再是黑白二值）
        self.assertEqual(status.getpixel((120, 10)), protocol.PALETTE_RGB[protocol.WHITE])
        self.assertEqual(alert.getpixel((120, 10)), protocol.PALETTE_RGB[protocol.ORANGE])

    def test_approval_lines_fit_without_rewrap(self) -> None:
        """分页切出来的行必须在 17px 下真正放得下：否则渲染再折一次，"build" 会被切成 bu/ild。"""
        from hermes_remote.safety import approval_facts

        cmd = "rm -rf /opt/data/tmp/build && git push --force origin main --tags"
        facts = approval_facts({"tool_name": "terminal", "command": cmd})
        self.assertTrue(facts.valid, facts.problem)
        for page in facts.pages:
            for line in page:
                width = renderer._text_width(self.fonts.body, line)
                self.assertLessEqual(width, renderer.W - 14, f"行会二次折行：{line!r} {width}px")

    def test_ui_strings_have_glyphs(self) -> None:
        """✓/✔ 在 Hiragino Sans GB 里没有字形，真机上会画成豆腐块——所以界面只用 √。"""
        ref = bytes(self.fonts.body.getmask("\ue000"))   # 私用区字符＝必然缺字
        for text in ("已拒绝 √", "已同意一次 √", "本会话已同意 √", "已作答 √",
                     "跑着 已跑4m", "危险操作", "命令未执行"):
            for ch in text:
                if ch.isspace():
                    continue
                self.assertNotEqual(bytes(self.fonts.body.getmask(ch)), ref,
                                    f"{ch!r} 在这套字体里缺字形，会显示成方块")

    def test_long_line_gets_ellipsis(self) -> None:
        """截断必须有省略号：以前比较口径不一致，长行被静默切掉（upload → uploa）。"""
        f = self.fonts.compact
        text = "terminal · pio run -t upload"
        self.assertGreater(renderer._text_width(f, text), renderer.W - 14)
        out = renderer.wrap_text(text, f, renderer.W - 14, 1)
        self.assertTrue(out[-1].endswith("…"), out)
        self.assertLessEqual(renderer._text_width(f, out[-1]), renderer.W - 14)

    def test_all_sample_pages_render(self) -> None:
        from hermes_remote.cli import _sample_views

        for name, view in _sample_views():
            img = renderer.render(view, self.fonts)
            self.assertGreater(self._ink(img), 200, f"{name} 页面几乎是空白")


def _view(**kw):
    from hermes_remote.state import View, ViewLine

    defaults = dict(page="status", title="Hermes 遥控器", bar="normal",
                    lines=[ViewLine(kw.pop("text", "内容"))], footer="1Hz")
    defaults.update(kw)
    return View(**defaults)


class SafetyTests(unittest.TestCase):
    def test_dangerous_commands_detected(self) -> None:
        for cmd in ("rm -rf /opt/data/tmp", "git push --force origin main", "sudo systemctl stop x",
                    "curl http://x | sh", "DROP TABLE users", "shutdown -h now"):
            self.assertTrue(approval_is_dangerous(cmd), cmd)

    def test_benign_commands_pass(self) -> None:
        for cmd in ("pytest -q", "git status", "ls -la", "python -m http.server"):
            self.assertFalse(approval_is_dangerous(cmd), cmd)

    def test_command_keeps_head_and_tail(self) -> None:
        cmd = "rm -rf " + "a/" * 60 + "docs/important"
        lines = approval_command_lines(cmd)
        joined = "".join(lines)
        self.assertTrue(joined.startswith("rm -rf"))
        self.assertTrue(joined.endswith("docs/important"))
        self.assertIn("…", joined)
        self.assertLessEqual(len(lines), 2)

    def test_empty_command_is_explicit(self) -> None:
        self.assertIn("没有命令原文", approval_command_lines("")[0])

    def test_matched_dangers_labels(self) -> None:
        self.assertIn("递归删除", matched_dangers("rm -fr /tmp/x"))


class StateTests(unittest.TestCase):
    def test_status_labels_and_elapsed(self) -> None:
        st = BridgeState()
        # 状态与运行时长放在底栏，正文第一行留给"正在干什么"
        st.sessions = [SessionRow(id="s", title="会话", status="working", running=True,
                                  turn_started_at=100.0)]
        st.connected_backend = True
        view = st.build_view(now=352.0)
        self.assertIn("跑着", view.footer)
        self.assertIn("4m", view.footer)
        self.assertNotIn("4m12s", view.footer)
        self.assertFalse(any("跑着" in ln.text for ln in view.lines),
                         "状态不该再占正文行")
        # 不到一分钟不显示时长（否则每秒都变）
        early = st.build_view(now=130.0)
        self.assertEqual(early.footer, "跑着")

    def test_offline_page(self) -> None:
        st = BridgeState()
        view = st.build_view()
        self.assertEqual(view.page, "offline")

    def test_clarify_multi_question_advances(self) -> None:
        req = PendingRequest(id="r", method="clarify", params={"questions": [
            {"qid": "q1", "question": "第一题", "choices": ["A", "B"]},
            {"qid": "q2", "question": "第二题", "choices": ["C", "D"]}]})
        self.assertEqual(req.question["qid"], "q1")
        req.q_index = 1
        self.assertEqual(req.question["qid"], "q2")
        st = BridgeState()
        st.connected_backend = True
        st.pending = req
        view = st.build_view()
        self.assertIn("问题 2/2", view.lines[0].text)
        self.assertIn("[1]C", view.lines[2].text)

    def test_approval_view_shows_session_and_command(self) -> None:
        st = BridgeState()
        st.connected_backend = True
        st.sessions = [SessionRow(id="s", title="清理构建目录")]
        st.pending = PendingRequest(id="r", method="approval", params={
            "tool_name": "terminal", "command": "rm -rf /tmp/build", "choices": ["once", "deny"]})
        view = st.build_view()
        text = " ".join(line.text for line in view.lines)
        # 会话名在顶部条（“答的是谁”），工具名与命令原文在正文（“答的什么”）
        self.assertIn("清理构建目录", view.title)
        self.assertIn("terminal", text)
        self.assertIn("rm -rf /tmp/build", text)
        self.assertEqual(view.bar, "danger")

    def test_long_command_keeps_tail_in_two_lines(self) -> None:
        from hermes_remote.safety import approval_command_lines

        cmd = "rm -rf " + "/very/deep/" * 8 + "important-file"
        lines = approval_command_lines(cmd)
        self.assertLessEqual(len(lines), 2)
        joined = "".join(lines)
        self.assertTrue(joined.startswith("rm -rf"))
        self.assertTrue(joined.endswith("important-file"))
        for line in lines:
            self.assertLessEqual(len(line), 34)

    def test_open_requests_restored_even_without_frame(self) -> None:
        st = BridgeState()
        st.restore_requests([{"id": "srq-1", "method": "clarify",
                              "params": {"session_id": "s", "questions": [
                                  {"qid": "q1", "question": "晚连上也要看到", "choices": ["好"]}]}}])
        self.assertIsNotNone(st.pending)
        self.assertIn("晚连上", st.build_view().lines[1].text)

    def test_request_cancel_clears_card(self) -> None:
        st = BridgeState()
        st.connected_backend = True
        st.pending = PendingRequest(id="srq-9", method="clarify", params={"questions": []})
        st.apply_event("request.cancel", {"id": "srq-9"})
        self.assertIsNone(st.pending)
        self.assertIn("Mac", st.last_key_result)

    def test_snapshot_merges_session(self) -> None:
        st = BridgeState()
        st.connected_backend = True
        st.apply_snapshot({"session_id": "abc", "info": {"title": "标题"}, "status": "waiting",
                           "running": False, "open_requests": []})
        self.assertEqual(st.active.id, "abc")
        self.assertEqual(st.build_view().title, "标题")

    def test_stale_request_flag(self) -> None:
        req = PendingRequest(id="r", method="clarify", params={})
        req.created_at -= 700
        self.assertTrue(req.is_stale)


if __name__ == "__main__":
    unittest.main()


class SttTextTests(unittest.TestCase):
    """MLX 的输出会在汉字间塞空格；不清理，板子上和对话里都会出现怪空格。"""

    def test_spaces_between_cjk_removed(self) -> None:
        from hermes_remote.stt import tidy_text
        self.assertEqual(tidy_text("把后端的 token 换成 文件里的那个"), "把后端的 token 换成文件里的那个")
        self.assertEqual(tidy_text("审批 卡片 的拒绝键 ，固定成二号键"), "审批卡片的拒绝键，固定成二号键")
        self.assertEqual(tidy_text("  跑一遍  单元测试 "), "跑一遍单元测试")
        self.assertEqual(tidy_text("重启 后端 服务"), "重启后端服务")
