"""M12 回归：日志落盘初始化必须容错，脏值/不可写目录不得让 bot 起不来。

对应评审 review/REVIEW-bbd8913..f6dffcc.md 的 M12。
"""

import logging
from pathlib import Path

import agentcore.logging_setup as mod


def _logger(name: str) -> logging.Logger:
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg


def test_dirty_keep_disables_without_raising(tmp_path, caplog):
    lg = _logger("t.dirty")
    assert mod.setup_file_logging(keep_raw="abc", log_dir=tmp_path, root=lg) is None
    assert "不是整数" in caplog.text
    assert not (tmp_path / "agent.log").exists()


def test_negative_keep_disables(tmp_path):
    assert (
        mod.setup_file_logging(keep_raw="-3", log_dir=tmp_path, root=_logger("t.neg"))
        is None
    )


def test_zero_disables_without_creating_file(tmp_path):
    assert (
        mod.setup_file_logging(keep_raw="0", log_dir=tmp_path, root=_logger("t.zero"))
        is None
    )
    assert not (tmp_path / "agent.log").exists()


def test_success_creates_file_and_writes(tmp_path):
    lg = _logger("t.ok")
    handler = mod.setup_file_logging(keep_raw="3", log_dir=tmp_path / "logs", root=lg)
    assert handler is not None
    try:
        lg.info("hello-from-test")
        handler.flush()
        content = (tmp_path / "logs" / "agent.log").read_text(encoding="utf-8")
        assert "hello-from-test" in content
    finally:
        lg.removeHandler(handler)
        handler.close()


def test_unwritable_dir_falls_back(monkeypatch, tmp_path, caplog):
    """目录建不出来时只降级，不抛（此前的顶层裸奔会让 python bot.py 直接崩）。"""

    def boom(*args, **kwargs):
        raise PermissionError("read-only cwd")

    monkeypatch.setattr(Path, "mkdir", boom)
    assert (
        mod.setup_file_logging(
            keep_raw="14", log_dir=tmp_path / "x", root=_logger("t.perm")
        )
        is None
    )
    assert "降级为仅控制台" in caplog.text


class TestLogContentSwitch:
    """M5（REVIEW-ee39c8e..e55cab5）：开关下沉到平台无关层，适配层与引擎共用。"""

    def test_default_keeps_truncated_body(self, monkeypatch):
        monkeypatch.delenv("AGENT_LOG_CONTENT", raising=False)
        assert mod.log_content_enabled() is True
        assert mod.log_text("这是一条消息") == "这是一条消息"
        assert mod.log_text("a" * 10, 4) == "aaaa..."

    def test_dirty_value_warns_and_falls_back_to_default(self, monkeypatch, caplog):
        """G（REVIEW 2026-11-02）：脏值不得静默按「开」——正文会继续进日志。

        旧手解 `raw not in {0,false,no,off}` 把 ``banana`` 判成 True 且无痕，
        与「关正文」的隐私意图正好相反。
        """
        monkeypatch.setenv("AGENT_LOG_CONTENT", "banana")
        assert mod.log_content_enabled() is True
        assert "不是布尔值" in caplog.text
        assert "banana" in caplog.text

    def test_off_redacts_to_length(self, monkeypatch):
        monkeypatch.setenv("AGENT_LOG_CONTENT", "0")
        assert mod.log_content_enabled() is False
        assert mod.log_text("机密正文") == "<len=4>"
        assert mod.log_text("机密正文", None) == "<len=4>"


def test_default_keep_is_fourteen(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_LOG_KEEP_DAYS", raising=False)
    lg = _logger("t.default")
    handler = mod.setup_file_logging(log_dir=tmp_path, root=lg)
    assert handler is not None
    assert handler.backupCount == 14
    lg.removeHandler(handler)
    handler.close()


class TestLoguruBridge:
    """框架日志必须进文件（2026-11-04 线上事故回归）。

    事故：`Running Matcher ... failed. AttributeError: ...` 只出现在 journald，
    `data/logs/agent.log` 里一条都没有——NoneBot 的 logger 就是全局 loguru
    （`nonebot/log.py: logger = loguru.logger`），直连 stdout，不经过 logging
    模块，root 上的文件 handler 天生收不到。故障因此隐形两个多小时。
    """

    def test_loguru_records_reach_the_file(self, tmp_path):
        import loguru

        lg = _logger("t.bridge")
        handler = logging.FileHandler(tmp_path / "agent.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter(mod.FORMAT))
        lg.addHandler(handler)
        assert mod.bridge_loguru(handler) is True

        try:
            loguru.logger.error("TESTMARKER framework failure")
        finally:
            loguru.logger.remove()

        text = (tmp_path / "agent.log").read_text(encoding="utf-8")
        assert "TESTMARKER framework failure" in text
        assert "ERROR" in text  # 级别必须透传，不能全掉成 INFO

    def test_bridge_failure_does_not_raise(self, monkeypatch, caplog):
        """桥接失败只告警——它是增益，不能影响 bot 启动。"""

        def _boom():
            raise RuntimeError("no loguru")

        monkeypatch.setattr(
            "builtins.__import__", lambda *a, **k: (_ for _ in ()).throw(_boom())
        )
        with caplog.at_level(logging.WARNING, logger="agentcore.logging_setup"):
            assert mod.bridge_loguru(logging.NullHandler()) is False
        assert "loguru 日志桥接失败" in caplog.text
