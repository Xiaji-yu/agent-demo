"""音频 → QQ silk 编码。

为什么自己转而不让协议端转：OneBot v11 兼容实现的 silk 能力依赖各自打包的
原生 ffmpeg addon（per-platform 二进制），Linux 上外部 ffmpeg 分支往往不支持
silk；自己产出 `\\x02#!SILK_V3` 后，实现侧的 ``isSilk`` 检查（``#!SILK`` /
``\\x02#!SILK``）会直接命中、不再转换，也就绕开了那层依赖。

编码速率基本只由时长决定、与音频内容无关（实测宽带噪声 2.12 KB/s、正弦音
2.25 KB/s），所以 5 分钟 ≈ 600 KB silk ≈ 800 KB base64，走反向 WS 不成问题。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# pysilk 只接受这些采样率；24 kHz 是 QQ 语音的标准取值
SUPPORTED_RATES = frozenset({8000, 12000, 16000, 24000, 32000, 48000})
TARGET_RATE = 24000

# L9：ffmpeg 子进程墙钟上限。正常转码（本地文件）远低于此；超时说明源文件
# 异常或 ffmpeg 卡死，宁可失败也不能永久占用工作线程。
_FFMPEG_TIMEOUT = 60.0

# silk 文件头：0x02 前缀 + #!SILK_V3，QQ/NapCat/SnowLuma 的 isSilk 都认这个形状
SILK_HEADER = b"\x02#!SILK_V3"
_HEADER_LEN = len(SILK_HEADER)

# M4：PCM 解码后驻留内存，极端文件（20 MB MP3 解出数十 MB PCM）可耗尽工作线程内存。
# 50 MB 对应约 300 秒 48 kHz 单声道 16-bit（约 22 分钟），超出即拒绝。
_MAX_PCM_BYTES = 50 * 1024 * 1024


def silk_available() -> tuple[bool, str]:
    """返回 (是否可用, 不可用原因)。探测 pysilk 与 ffmpeg，不抛异常。

    注册闸门用它决定是否装配点歌路由——缺任一项都不注册，而不是等运行时才炸。
    """
    if shutil.which("ffmpeg") is None:
        return False, "ffmpeg 不在 PATH"
    try:
        import pysilk  # noqa: F401
    except Exception as e:  # pragma: no cover - 依赖缺失分支
        return False, f"pysilk 不可导入：{type(e).__name__}"
    return True, ""


def _ffmpeg_to_wav(src: Path, dst: Path) -> None:
    """任意音频 → 24 kHz 单声道 16-bit PCM wav。

    argv 列表、不经 shell：源路径由本模块自己下载得到，但沿用仓库
    ``workspace/runner.py`` 的铁律，绝不把路径交给 shell 解释。
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        str(TARGET_RATE),
        "-ac",
        "1",
        str(dst),
    ]
    # L9（REVIEW-6ec3f7c..a36ea1d）：必须有 timeout。旧实现无上限——一个坏文件
    # 可以让 ffmpeg 永久挂住一个工作线程（_play 也没有整体超时），
    # 而 to_thread 的线程池有限，挂满即全面失去卸载能力。
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg 转码超时（>{_FFMPEG_TIMEOUT:.0f}s）：{src}") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 转码失败（rc={proc.returncode}）：{proc.stderr[:200]}"
        )


def _pcm_from_wav(path: Path) -> tuple[bytes, int]:
    """返回 ``(pcm, 实际采样率)``。

    采样率必须取自文件头而不是假设它等于 TARGET_RATE：编码速率若与数据
    实际速率不一致，产出会变速变调。让编码永远跟随文件真实速率，
    这类不一致在结构上就不可能发生。
    """
    import wave

    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        pcm = w.readframes(w.getnframes())
    if len(pcm) > _MAX_PCM_BYTES:
        raise RuntimeError(
            f"PCM 数据过大（{len(pcm)} 字节 > {_MAX_PCM_BYTES}），源文件过长或格式异常"
        )
    if rate not in SUPPORTED_RATES:
        raise RuntimeError(
            f"wav 采样率 {rate} 不被 silk 支持（须为 {sorted(SUPPORTED_RATES)}）"
        )
    if channels != 1:
        raise RuntimeError(f"wav 声道数 {channels} 须为 1")
    if sampwidth != 2:
        raise RuntimeError(f"wav 位宽 {sampwidth} 须为 2（16-bit PCM）")
    return pcm, rate


def _encode_pcm_to_silk(pcm: bytes, rate: int) -> bytes:
    """pysilk 的纯 CPU 编码。tencent=True 产出 QQ 认的 0x02 前缀 SILK_V3。"""
    import io

    import pysilk

    out = io.BytesIO()
    pysilk.encode(io.BytesIO(pcm), out, rate, rate, tencent=True)
    return out.getvalue()


async def encode_to_silk(src: Path) -> bytes:
    """把任意音频文件编成 QQ 语音 silk 字节。

    ffmpeg 走子进程（不阻塞事件循环），pysilk 是纯 CPU 故卸载到线程——
    与 ``outbound`` 渲染表格图片同理：同步 CPU 留在事件循环里会冻结所有会话。
    """
    ok, reason = silk_available()
    if not ok:
        raise RuntimeError(f"silk 编码不可用：{reason}")

    with tempfile.TemporaryDirectory(prefix="dsh-music-") as tmp:
        wav = Path(tmp) / "audio.wav"
        # ffmpeg 起子进程本身不阻塞循环，但它会写盘；整段放线程更省心
        await asyncio.to_thread(_ffmpeg_to_wav, Path(src), wav)
        pcm, rate = await asyncio.to_thread(_pcm_from_wav, wav)
        if not pcm:
            raise RuntimeError("音频解码后为空（源文件损坏或格式不支持）")
        silk = await asyncio.to_thread(_encode_pcm_to_silk, pcm, rate)

    if not silk:
        raise RuntimeError("silk 编码结果为空")
    if not silk.startswith(SILK_HEADER):
        raise RuntimeError(f"silk 产物头部异常：{silk[:9]!r}（期望 {SILK_HEADER!r}）")
    logger.info(
        "silk 编码完成：%d 字节 PCM → %d 字节 silk（%.2f KB/s）",
        len(pcm),
        len(silk),
        len(silk) * rate / max(1, len(pcm) / 2) / 1024,
    )
    return silk
