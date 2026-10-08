import json

import pytest

import main
from main import ConfigError, build_command, load_config


def test_load_valid_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"listen_port": 9999}), encoding="utf-8")
    assert load_config(path)["listen_port"] == 9999


def test_rejects_invalid_upstream_type(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"upstream_proxy": {"enabled": True, "host": "127.0.0.1", "port": 1, "type": "ftp"}}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_builds_upstream_mode():
    config = {"listen_host": "127.0.0.1", "listen_port": 8888, "upstream_proxy": {"enabled": True, "host": "127.0.0.1", "port": 10808, "type": "http"}}
    command = build_command(config, False, "mitmdump")
    assert "upstream:http://127.0.0.1:10808" in command

def test_finds_mitmdump_next_to_virtualenv_python(tmp_path, monkeypatch):
    executable = tmp_path / "python.exe"
    mitmdump = tmp_path / "mitmdump.exe"
    executable.touch()
    mitmdump.touch()
    monkeypatch.setattr(main.shutil, "which", lambda _: None)
    monkeypatch.setattr(main.sys, "executable", str(executable))
    monkeypatch.setattr(main.os, "name", "nt")
    assert main.find_mitmdump() == str(mitmdump)