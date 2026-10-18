"""睡眠抑制：有任务时别让 Mac 睡过去。

Mac 睡了，跑在上面的 Hermes 本身就停了——遥控器救不了这件事。
所以桥活着的时候用 ``caffeinate -dims -w <pid>`` 顶住系统睡眠
（有显示器时 -d，系统 -i，磁盘 -m，-s 插电时也顶）；桥退出，caffeinate 跟着退出。

合盖这条绕不过去：要么开盖，要么接外显并插电（clamshell）。这条如实写在文档里，
不靠代码假装解决。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger("hermes_remote.power")


class SleepGuard:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.proc: subprocess.Popen | None = None

    def available(self) -> bool:
        return sys.platform == "darwin" and shutil.which("caffeinate") is not None

    def start(self) -> "SleepGuard":
        if not self.enabled:
            return self
        if not self.available():
            log.info("caffeinate 不可用（非 macOS？），跳过睡眠抑制")
            return self
        try:
            self.proc = subprocess.Popen(
                ["caffeinate", "-dims", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("sleep guard on (caffeinate pid=%d)", self.proc.pid)
        except OSError as exc:
            log.warning("caffeinate 启动失败：%s", exc)
        return self

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def describe(self) -> str:
        if not self.enabled:
            return "已关闭（--no-keep-awake）"
        if not self.available():
            return "不可用（不是 macOS 或没有 caffeinate）"
        return f"运行中 pid={self.proc.pid}" if self.proc else "未启动"


def pmset_summary() -> str:
    """把当前电源睡眠设置总结成一句话，doctor 用。"""
    if sys.platform != "darwin":
        return "非 macOS，跳过"
    try:
        out = subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f"读不到 pmset：{exc}"
    keys = {}
    for line in out.splitlines():
        for key in ("sleep", "displaysleep", "lidwake"):
            if line.strip().startswith(key):
                keys[key] = line.split()[-1]
    if not keys:
        return "读到 pmset，但没解析出睡眠项"
    return (f"sleep={keys.get('sleep', '?')}min displaysleep={keys.get('displaysleep', '?')}min "
            f"lidwake={keys.get('lidwake', '?')}；合盖仍会睡，接电+外显才是长久解")
