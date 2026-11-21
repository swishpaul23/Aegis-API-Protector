import pytest

from aegis_healer.meaning.config import MeaningConfig, load_config


def test_policy_precedence():
    cfg = MeaningConfig(default="flag", fields={"app.User.amount": "off", "amount": "block"})
    assert cfg.policy_for("app.User", "amount") == "off"  # exact key wins
    assert cfg.policy_for("app.Other", "amount") == "block"  # bare path next
    assert cfg.policy_for("app.User", "price") == "flag"  # default last


def test_default_is_block():
    assert MeaningConfig().policy_for("m", "p") == "block"


@pytest.mark.parametrize("policy", ["block", "flag", "off"])
def test_load_from_toml(tmp_path, policy):
    (tmp_path / "config.toml").write_text(
        f'[meaning]\ndefault = "{policy}"\n\n[meaning.fields]\n"app.User.amount" = "off"\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.default == policy
    assert cfg.policy_for("app.User", "amount") == "off"


def test_missing_file_and_table_give_defaults(tmp_path):
    assert load_config(tmp_path) == MeaningConfig()
    (tmp_path / "config.toml").write_text("[other]\nx = 1\n", encoding="utf-8")
    assert load_config(tmp_path) == MeaningConfig()


def test_invalid_policy_names_key_not_value(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[meaning.fields]\n"app.User.amount" = "SECRETVALUE"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError) as e:
        load_config(tmp_path)
    assert "app.User.amount" in str(e.value)
    assert "SECRETVALUE" not in str(e.value)
    assert "config.toml" in str(e.value)


def test_invalid_default_names_default(tmp_path):
    (tmp_path / "config.toml").write_text('[meaning]\ndefault = "nope"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="default") as e:
        load_config(tmp_path)
    assert "nope" not in str(e.value)


def test_invalid_toml(tmp_path):
    (tmp_path / "config.toml").write_text("[meaning\nsecret = ", encoding="utf-8")
    with pytest.raises(ValueError, match="config.toml is not valid TOML") as e:
        load_config(tmp_path)
    assert "secret" not in str(e.value)


def test_non_utf8_config_is_invalid_toml(tmp_path):
    (tmp_path / "config.toml").write_bytes(b"\xff\xfe[meaning]\n\x80")
    with pytest.raises(ValueError, match="is not valid TOML"):
        load_config(tmp_path)
