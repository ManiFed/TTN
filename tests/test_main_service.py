import pathlib
from unittest import mock

from src import main_service


def test_prepare_data_dir_seeds_writable_config(tmp_path):
    template = tmp_path / "template.yaml"
    template.write_text(
        "cloud:\n  activation_code: ACTIVATION_CODE_PLACEHOLDER\n",
        encoding="utf-8",
    )
    data_dir = tmp_path / "runtime"

    with mock.patch.object(main_service, "_template_path", return_value=template):
        main_service._prepare_data_dir(data_dir)

    config = data_dir / "config.yaml"
    assert config.is_file()
    assert "ACTIVATION_CODE_PLACEHOLDER" not in config.read_text(encoding="utf-8")
    assert config.stat().st_mode & 0o222
    assert (data_dir / "logs").is_dir()


def test_prepare_data_dir_preserves_existing_config(tmp_path):
    data_dir = tmp_path / "runtime"
    data_dir.mkdir()
    config = data_dir / "config.yaml"
    config.write_text("custom: true\n", encoding="utf-8")

    main_service._prepare_data_dir(data_dir)

    assert config.read_text(encoding="utf-8") == "custom: true\n"


def test_macos_default_data_dir_is_in_user_library():
    with (
        mock.patch.object(main_service.sys, "platform", "darwin"),
        mock.patch.object(pathlib.Path, "home", return_value=pathlib.Path("/Users/member")),
    ):
        assert main_service._default_data_dir() == pathlib.Path(
            "/Users/member/Library/Application Support/TelescopeNet/NodeAgent"
        )
