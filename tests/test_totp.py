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

    def test_empty_env_falls_back_to_default(self, monkeypatch, tmp_path):
        """M18（REVIEW-1b16fa7..9c2d3c1）：env 空串（`KEY=` 的 dotenv 注入形态）
        必须回落默认路径——旧实现 `os.getenv(ENV, default)` 空串不回落 →
        Path("") = "." → 写分支 ValueError 逃逸 → /2fa 无响应 + web 写面全 500。
        """
        monkeypatch.setenv("AGENT_2FA_SECRET_FILE", "")
        monkeypatch.setattr(totp, "_DEFAULT_PATH", str(tmp_path / "d" / "2fa.json"))
        p = totp._secret_path()
        assert p == tmp_path / "d" / "2fa.json"
        s = totp.load_or_create_secret()  # 不得抛
        assert len(s) == 32
        assert (tmp_path / "d" / "2fa.json").exists()

    def test_non_base32_secret_regenerates(self, secret_file):
        """L2：JSON 合法但 secret 非 base32（手工损坏）时按损坏重生成——
        否则 verify/current_code 抛 binascii.Error，/2fa 与全部写路由 500 死锁。
        """
        secret_file.write_text('{"secret": "abc@@@"}', encoding="utf-8")
        s = totp.load_or_create_secret()
        assert len(s) == 32
        assert totp.verify(s, totp.current_code(s, 1_000_000_000), 1_000_000_000)


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
        # L1：取一个确定非当前码的错码——旧写法 `not verify(...) or current==...`
        # 在当前码恰为该串时恒真（1/10⁶ 漏抓窗），变异测试可能假阴性
        wrong = "000000" if totp.current_code(s, t) != "000000" else "111111"
        assert not totp.verify(s, wrong, t)
        assert not totp.verify(s, "abc", t)
        assert not totp.verify(s, "12345", t)  # 非 6 位

    def test_window_tolerance(self):
        s = totp.generate_secret()
        t = 1_000_000_000
        assert totp.verify(s, totp.current_code(s, t - 300), t)  # 上一窗
        assert totp.verify(s, totp.current_code(s, t + 300), t)  # 下一窗
        assert not totp.verify(s, totp.current_code(s, t - 1200), t)  # 超 4 窗
