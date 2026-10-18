"""本地语音转文字：faster-whisper，音频不出网。

只在 Mac 上跑。第一次用会下载模型（turbo ≈ 1.6GB），之后离线可用。
测试和 demo 用 ``FakeTranscriber``，不碰模型。
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("hermes_remote.stt")

DEFAULT_MODEL = os.environ.get("HERMES_REMOTE_STT_MODEL", "large-v3-turbo")
#: Apple Silicon 上优先走 MLX（同一模型 1.2s vs CPU 6.5s）；设成 ct2 可强制回 CPU 实现
BACKEND = os.environ.get("HERMES_REMOTE_STT_BACKEND", "auto").lower()
MLX_REPO = os.environ.get("HERMES_REMOTE_STT_MLX_REPO", "mlx-community/whisper-large-v3-turbo")

#: whisper 用 initial_prompt 做“热词偏置”：不带这段，中文模型会把 token 听成“抽通/周根”。
#: 只在提示里放**词**，不放句子——放句子会被模型当成前文续写出来。
DEFAULT_PROMPT = os.environ.get(
    "HERMES_REMOTE_STT_PROMPT",
    "以下是普通话。Hermes token 会话 审批 拒绝 同意 提交 合并 部署 回滚 日志 后端 前端 "
    "接口 测试 方案 思路 重构 分支 提交 环境 配置 脚本 端口 模型 请求 超时。",
)


_CJK = r"\u4e00-\u9fff"


def tidy_text(text: str) -> str:
    """MLX 会在汉字之间塞空格（"换成 文件里"）：中文之间、中文与标点之间的空格一律去掉。"""
    text = re.sub(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])", "", text)
    text = re.sub(rf"\s+(?=[，。！？、；：,.!?;:])", "", text)
    text = re.sub(rf"(?<=[，。！？、；：])\s+", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


class TranscriptionUnavailable(RuntimeError):
    pass


class Transcriber:
    """懒加载的 faster-whisper 封装。"""

    def __init__(self, model: str = DEFAULT_MODEL, device: str = "auto",
                 compute_type: str = "int8") -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self._model = None
        self._load_error: str | None = None
        self._mlx = None            # mlx_whisper 模块（Apple GPU 路径）
        self._backend: str | None = None

    # ── 可用性 ──────────────────────────────────────────────────────────────
    def preflight(self) -> tuple[bool, str]:
        if os.environ.get("HERMES_REMOTE_STT") == "off":
            return False, "HERMES_REMOTE_STT=off"
        try:
            import faster_whisper  # noqa: F401
        except Exception as exc:
            return False, f"faster-whisper 没装：{exc}"
        try:
            import numpy  # noqa: F401
        except Exception as exc:
            return False, f"numpy 没装：{exc}"
        return True, "ok"

    # ── 后端选择 ────────────────────────────────────────────────────────────
    def _ensure_backend(self) -> str:
        """挑后端：Apple Silicon 上 mlx-whisper 快 5 倍以上，其余走 faster-whisper。"""
        if self._backend:
            return self._backend
        if BACKEND in ("auto", "mlx"):
            try:
                import platform

                # 关键：mlx-whisper 会连带 import torch，而本机走 fake-ip 代理时
                # torch 的首次 import 会卡 150 秒（实测）。导入期间临时摘掉代理变量。
                _PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy",
                               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
                saved = {k: os.environ.pop(k) for k in _PROXY_KEYS if k in os.environ}
                try:
                    import mlx_whisper
                finally:
                    os.environ.update(saved)
                if platform.system() == "Darwin" and platform.machine() == "arm64":
                    self._mlx = mlx_whisper
                    self._backend = "mlx"
                    return self._backend
            except Exception as exc:            # noqa: BLE001
                if BACKEND == "mlx":
                    raise TranscriptionUnavailable(f"mlx-whisper 不可用：{exc}") from exc
                log.info("mlx-whisper 不可用，回落 faster-whisper：%s", exc)
        self._backend = "ct2"
        return self._backend

    def warmup(self) -> tuple[bool, str]:
        """把模型和推理图提前跑热：第一次调用要 40s+，不能让人等到按完键还在等。"""
        try:
            self._ensure_backend()
            if self._backend == "mlx":
                import numpy as np
                silence = np.zeros(8000, dtype="float32")   # 0.5s 静音
                self._mlx.transcribe(silence, path_or_hf_repo=MLX_REPO, language="zh",
                                     initial_prompt=DEFAULT_PROMPT)
                return True, "mlx warm"
            self._ensure_model()
            return True, "ct2 warm"
        except Exception as exc:                # noqa: BLE001
            return False, str(exc)

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        ok, why = self.preflight()
        if not ok:
            raise TranscriptionUnavailable(why)
        from faster_whisper import WhisperModel

        device = self.device
        if device == "auto":
            device = "cpu"
            try:
                import platform
                if platform.system() == "Darwin" and platform.machine() == "arm64":
                    device = "cpu"  # faster-whisper 在 Apple Silicon 上走 CPU + int8 最稳
            except Exception:
                pass
        log.info("loading whisper model %s on %s/%s", self.model_name, device, self.compute_type)
        try:
            self._model = WhisperModel(self.model_name, device=device,
                                       compute_type=self.compute_type)
        except Exception as exc:
            self._load_error = str(exc)
            raise TranscriptionUnavailable(f"模型加载失败：{exc}") from exc
        return self._model

    # ── 转写 ────────────────────────────────────────────────────────────────
    def transcribe(self, pcm: bytes, *, rate: int = 16000, language: str = "zh",
                   prompt_extra: str = "") -> str:
        if not pcm:
            return ""
        import numpy as np

        audio = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
        # 板子麦克风电平偏低：先按峰值归一到 0.9，再交给模型（安静句也能识别）
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if 0.0 < peak < 0.9:
            audio = audio * (0.9 / peak)

        backend = self._ensure_backend()
        # 把"当前对话里出现过的词"拼进提示：专有名词、路径、人名因此更容易听对
        prompt = DEFAULT_PROMPT
        extra = " ".join((prompt_extra or "").split())[:160]
        if extra:
            prompt = f"{prompt} {extra}"
        if backend == "mlx":
            out = self._mlx.transcribe(audio, path_or_hf_repo=MLX_REPO, language=language,
                                       initial_prompt=prompt, condition_on_previous_text=False,
                                       temperature=0.0)
            return tidy_text(str(out.get("text") or ""))

        model = self._ensure_model()
        segments, _info = model.transcribe(audio, language=language, beam_size=5,
                                           vad_filter=True, initial_prompt=prompt,
                                           condition_on_previous_text=False)
        return tidy_text("".join(seg.text for seg in segments))


class FakeTranscriber:
    """测试/demo 用：把“音频”按固定话术返回，同时记录收到的长度。"""

    def __init__(self, text: str = "这个方案不行，换个思路") -> None:
        self.text = text
        self.calls: list[int] = []

    def preflight(self) -> tuple[bool, str]:
        return True, "fake"

    def transcribe(self, pcm: bytes, *, rate: int = 16000, language: str = "zh",
                   prompt_extra: str = "") -> str:
        self.calls.append(len(pcm))
        return self.text


def make_transcriber(kind: str | None = None):
    kind = kind or os.environ.get("HERMES_REMOTE_STT", "faster-whisper")
    if kind in ("fake", "off"):
        return FakeTranscriber()
    return Transcriber()
