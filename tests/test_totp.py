"""TOTP 双因素认证：secret 管理 / 滚动码生成 / 验证 / 窗口容忍。"""

import stat

import pytest

from agentcore import totp


@pytest.fixture
def secret_file(tmp_path, monkeypatch):
    p = tmp_path / "2fa-secret.json"
    monkeypatch.setenv("AGENT_2FA_SECRET_FILE", str(p))
    return p


class TestSecret:
    def test_generate_is_base32(self):
        s = totp.generate_secret()
        assert len(s) == 32
        assert s.isascii() and s.isalnum() and s.isupper()

    def test_load_or_create_0600_and_idempotent(self, secret_file):
        s1 = totp.load_or_create_secret()
        assert secret_file.exists()
        assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
        assert totp.load_or_create_secret() == s1  # 幂等

    def test_corrupt_file_regenerates(self, secret_file):
        secret_file.write_text("{bad json", encoding="utf-8")
        s = totp.load_or_create_secret()
        assert len(s) == 32
        assert totp.load_or_create_secret() == s


class TestCode:
    def test_current_code_is_6_digits(self):
        s = totp.generate_secret()
        code = totp.current_code(s, 1_000_000_000)
        assert len(code) == 6 and code.isdigit()

    def test_verify_current(self):
        s = totp.generate_secret()
        t = 1_000_000_000
        assert totp.verify(s, totp.current_code(s, t), t)

    def test_verify_rejects_wrong(self):
        s = totp.generate_secret()
        t = 1_000_000_000
        assert not totp.verify(s, "000000", t) or totp.current_code(s, t) == "000000"
        assert not totp.verify(s, "abc", t)
        assert not totp.verify(s, "12345", t)  # 非 6 位

    def test_window_tolerance(self):
        s = totp.generate_secret()
        t = 1_000_000_000
        assert totp.verify(s, totp.current_code(s, t - 300), t)  # 上一窗
        assert totp.verify(s, totp.current_code(s, t + 300), t)  # 下一窗
        assert not totp.verify(s, totp.current_code(s, t - 1200), t)  # 超 4 窗
