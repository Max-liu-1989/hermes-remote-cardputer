"""端到端测试：假后端 ↔ 桥 ↔ 模拟板子，全程走真 socket。

覆盖的正是验收清单上的动作：
  * 按 1 放行一个 clarify，后端拿到的就是选项原文；
  * 多题 clarify 逐题作答；
  * 危险命令按一次不算数，必须按两次；
  * Mac 上先答了 → 板子的卡片收掉，不重复提交；
  * 断线重连后板子立刻拿到最后一帧；
  * 语音必须“预览 → 确认”才发出去；
  * 过期请求不接受按键；
  * 错误的设备 token 连不上。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_remote.audit import AuditLog  # noqa: E402
from hermes_remote.bridge import Bridge, BridgeConfig  # noqa: E402
from hermes_remote.devsim import SimDevice  # noqa: E402
from hermes_remote.fake_gateway import FakeGateway  # noqa: E402
from hermes_remote.stt import FakeTranscriber  # noqa: E402

TOKEN = "test-device-token-0123456789abcdef"


class Harness:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.audit_path = Path(self.tmp.name) / "audit.jsonl"
        self.gw = FakeGateway(session_title="测试会话")
        self.bridge: Bridge | None = None
        self.dev: SimDevice | None = None

    async def start(self, *, transcriber=None) -> None:
        port = await self.gw.start()
        cfg = BridgeConfig(backend_url=self.gw.url, lan_port=0, device_token=TOKEN,
                           render_hz=4.0, snapshot_refresh_s=60, event_poll_s=0.5,
                           beacon=False)
        self.bridge = Bridge(cfg, transcriber=transcriber, audit=AuditLog(self.audit_path))
        await self.bridge.start()
        self.dev = SimDevice(port=self.bridge.hub.port, token=TOKEN)
        await asyncio.sleep(0.3)
        await self.dev.connect()
        await self.wait_for(lambda: self.bridge.state.connected_backend, 5)

    async def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return False

    async def stop(self) -> None:
        if self.dev:
            await self.dev.close()
        if self.bridge:
            await self.bridge.stop()
        await self.gw.stop()
        self.tmp.cleanup()

    def view(self):
        return self.bridge.state.build_view()


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.h = Harness()

    async def asyncTearDown(self) -> None:
        await self.h.stop()

    async def test_status_screen_arrives(self) -> None:
        await self.h.start()
        await self.h.gw.emit("status.update", kind="status", text="正在读 server.py")
        self.assertTrue(await self.h.wait_for(
            lambda: self.h.view().lines[0].text == "正在读 server.py", 3))
        frame = await self.h.dev.wait_frame(2)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.width, 240)
        self.assertEqual(frame.height, 135)

    async def test_clarify_answered_by_number_key(self) -> None:
        await self.h.start()
        task = asyncio.create_task(self.h.gw.ask("clarify", {"questions": [
            {"qid": "q1", "question": "挂上去还是另起？", "choices": ["挂上去", "另起一个"]}]}))
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "prompt", 3))
        await self.h.dev.key("1")
        result = await asyncio.wait_for(task, 5)
        self.assertEqual(result, {"answers": {"q1": "挂上去"}})
        # 答完之后先出确认页（“已作答 ✓”），几秒后回状态页
        self.assertTrue(await self.h.wait_for(
            lambda: self.h.view().page == "confirm", 3))
        self.assertTrue(await self.h.wait_for(
            lambda: self.h.view().page == "status", 6))

    async def test_multi_question_clarify_collects_all_answers(self) -> None:
        await self.h.start()
        task = asyncio.create_task(self.h.gw.ask("clarify", {"questions": [
            {"qid": "q1", "question": "第一题", "choices": ["A", "B"]},
            {"qid": "q2", "question": "第二题", "choices": ["C", "D"]}]}))
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "prompt", 3))
        await self.h.dev.key("2")
        self.assertTrue(await self.h.wait_for(
            lambda: "问题 2/2" in self.h.view().lines[0].text, 3))
        await self.h.dev.key("1")
        result = await asyncio.wait_for(task, 5)
        self.assertEqual(result, {"answers": {"q1": "B", "q2": "C"}})

    async def test_dangerous_approval_needs_two_presses(self) -> None:
        await self.h.start()
        task = asyncio.create_task(self.h.gw.ask("approval", {
            "request_id": "ap-1", "tool_name": "terminal",
            "command": "rm -rf /opt/data/tmp/build", "choices": ["once", "deny"]}))
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "prompt", 3))
        await self.h.dev.key("1")  # 上膛
        await asyncio.sleep(0.3)
        self.assertFalse(task.done(), "危险命令按一次就放行了，这是不可接受的")
        self.assertTrue(await self.h.wait_for(lambda: self.h.bridge.state.pending.confirm_armed, 2))
        await self.h.dev.key("1")  # 确认
        result = await asyncio.wait_for(task, 5)
        self.assertEqual(result, {"choice": "once"})

    async def test_deny_is_immediate_even_when_dangerous(self) -> None:
        await self.h.start()
        task = asyncio.create_task(self.h.gw.ask("approval", {
            "request_id": "ap-2", "tool_name": "terminal",
            "command": "sudo rm -rf /", "choices": ["once", "deny"]}))
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "prompt", 3))
        await self.h.dev.key("2")
        result = await asyncio.wait_for(task, 5)
        self.assertEqual(result, {"choice": "deny"})

    async def test_benign_approval_is_immediate(self) -> None:
        await self.h.start()
        task = asyncio.create_task(self.h.gw.ask("approval", {
            "request_id": "ap-3", "tool_name": "terminal",
            "command": "pytest -q", "choices": ["once", "deny"]}))
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "prompt", 3))
        await self.h.dev.key("1")
        result = await asyncio.wait_for(task, 5)
        self.assertEqual(result, {"choice": "once"})

    async def test_card_disappears_when_mac_answers_first(self) -> None:
        await self.h.start()
        task = asyncio.create_task(self.h.gw.ask("clarify", {"questions": [
            {"qid": "q1", "question": "会被 Mac 先答", "choices": ["A", "B"]}]}))
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "prompt", 3))
        rid = next(iter(self.h.gw.open_requests))
        self.h.gw.answers.append((rid, {"answers": {"q1": "A"}}))
        self.h.gw.open_requests.pop(rid, None)
        await self.h.gw.emit("request.cancel", id=rid, method="clarify",
                             reason="answered-elsewhere")
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "status", 3))
        # 板子再按 1 不应该再发出任何答案（不重复提交）
        before = len(self.h.gw.answers)
        await self.h.dev.key("1")
        await asyncio.sleep(0.3)
        self.assertEqual(len(self.h.gw.answers), before)
        await asyncio.wait_for(task, 5)

    async def test_stale_request_ignores_keys(self) -> None:
        await self.h.start()
        task = asyncio.create_task(self.h.gw.ask("clarify", {"questions": [
            {"qid": "q1", "question": "过期了", "choices": ["A"]}]}))
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "prompt", 3))
        self.h.bridge.state.pending.created_at -= 700
        await self.h.dev.key("1")
        await asyncio.sleep(0.3)
        self.assertFalse(task.done(), "过期请求不该被接受")
        self.assertIn("过期", self.h.bridge.state.last_key_result)

    async def test_voice_requires_confirmation(self) -> None:
        await self.h.start(transcriber=FakeTranscriber("这个方案不行，换个思路"))
        task = asyncio.create_task(self.h.gw.ask("clarify", {"questions": [
            {"qid": "q1", "question": "语音这块怎么做？", "choices": None}]}))
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "prompt", 3))
        resp = await self.h.dev.upload_voice(b"\x01\x02" * 4000)
        self.assertEqual(resp["text"], "这个方案不行，换个思路")
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "voice", 3))
        await asyncio.sleep(0.3)
        self.assertFalse(task.done(), "预览阶段就发出去了，不行")
        await self.h.dev.key("enter")
        result = await asyncio.wait_for(task, 5)
        self.assertEqual(result, {"answers": {"q1": "这个方案不行，换个思路"}})

    async def test_voice_cancel_sends_nothing(self) -> None:
        await self.h.start(transcriber=FakeTranscriber("听错的一句话"))
        task = asyncio.create_task(self.h.gw.ask("clarify", {"questions": [
            {"qid": "q1", "question": "随便", "choices": None}]}))
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "prompt", 3))
        await self.h.dev.upload_voice(b"\x00" * 1000)
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "voice", 3))
        await self.h.dev.key("esc")
        await asyncio.sleep(0.3)
        self.assertFalse(task.done())
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "prompt", 3))
        task.cancel()

    async def test_streamed_voice_from_board_reaches_confirm(self) -> None:
        """板子按真实做法边录边发（二进制分片），桥仍要先预览再确认。"""
        await self.h.start(transcriber=FakeTranscriber("把后端的 token 换成文件里的"))
        task = asyncio.create_task(self.h.gw.ask("clarify", {"questions": [
            {"qid": "q1", "question": "下一步？", "choices": None}]}))
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "prompt", 3))
        await self.h.dev.stream_voice(b"\x10\x20" * 8000)  # 1 秒 16k 音频，5 片
        # 录音/识别中/预览都用 voice 页，所以这里等的是“转写结果真的到了”
        self.assertTrue(await self.h.wait_for(
            lambda: bool(self.h.bridge.state.voice_preview), 5))
        self.assertEqual(self.h.view().page, "voice")
        self.assertIn("token", self.h.bridge.state.voice_preview)
        await self.h.dev.key("enter")
        result = await asyncio.wait_for(task, 5)
        self.assertEqual(result, {"answers": {"q1": "把后端的 token 换成文件里的"}})

    async def test_reconnect_pushes_last_frame_immediately(self) -> None:
        await self.h.start()
        first = await self.h.dev.wait_frame(3)
        self.assertIsNotNone(first)
        await self.h.dev.close()
        await asyncio.sleep(0.2)
        dev2 = SimDevice(port=self.h.bridge.hub.port, token=TOKEN)
        await dev2.connect()
        # 连上时桥会把缓存的最后一帧直接推过来（可能比 wait_frame 的基线更早到）
        await asyncio.sleep(0.6)
        self.assertTrue(dev2.frames, "重连后没有立刻收到最后一屏")
        frame = dev2.frames[-1]
        self.assertEqual(frame.seq, self.h.bridge.seq & 0xFFFF)
        await dev2.close()
        self.h.dev = None

    async def test_wrong_token_is_rejected(self) -> None:
        await self.h.start()
        bad = SimDevice(port=self.h.bridge.hub.port, token="wrong-token")
        with self.assertRaises(Exception):
            await bad.connect()
        await bad.close()

    async def test_audit_log_records_decision(self) -> None:
        await self.h.start()
        task = asyncio.create_task(self.h.gw.ask("clarify", {"questions": [
            {"qid": "q1", "question": "记一笔", "choices": ["好"]}]}))
        self.assertTrue(await self.h.wait_for(lambda: self.h.view().page == "prompt", 3))
        await self.h.dev.key("1")
        await asyncio.wait_for(task, 5)
        await asyncio.sleep(0.2)
        records = [json.loads(line) for line in
                   self.h.audit_path.read_text(encoding="utf-8").splitlines()]
        events = [r["event"] for r in records]
        self.assertIn("request.received", events)
        self.assertIn("request.answered", events)
        answered = [r for r in records if r["event"] == "request.answered"][0]
        self.assertEqual(answered["answers"], {"q1": "好"})
        self.assertEqual(answered["via"], "device")


if __name__ == "__main__":
    unittest.main()
