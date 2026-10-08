from __future__ import annotations

import ctypes
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

if os.name == "nt":
    import winreg

INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37


@dataclass
class ProxyState:
    proxy_enable: int = 0
    proxy_server: str | None = None
    proxy_override: str | None = None
    auto_config_url: str | None = None
    auto_detect: int | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProxyState":
        return cls(
            proxy_enable=int(value.get("proxy_enable", 0)),
            proxy_server=value.get("proxy_server"),
            proxy_override=value.get("proxy_override"),
            auto_config_url=value.get("auto_config_url"),
            auto_detect=value.get("auto_detect"),
        )


def _notify_windows() -> None:
    internet_set_option = ctypes.windll.Wininet.InternetSetOptionW
    internet_set_option(None, INTERNET_OPTION_SETTINGS_CHANGED, None, 0)
    internet_set_option(None, INTERNET_OPTION_REFRESH, None, 0)


def read_proxy_state() -> ProxyState:
    if os.name != "nt":
        raise OSError("Windows proxy settings are only available on Windows")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS) as key:
        values: dict[str, Any] = {}
        for name, field in (
            ("ProxyEnable", "proxy_enable"),
            ("ProxyServer", "proxy_server"),
            ("ProxyOverride", "proxy_override"),
            ("AutoConfigURL", "auto_config_url"),
            ("AutoDetect", "auto_detect"),
        ):
            try:
                values[field] = winreg.QueryValueEx(key, name)[0]
            except FileNotFoundError:
                values[field] = None
    values["proxy_enable"] = int(values["proxy_enable"] or 0)
    return ProxyState.from_dict(values)


def write_proxy_state(state: ProxyState) -> None:
    if os.name != "nt":
        raise OSError("Windows proxy settings are only available on Windows")
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, int(state.proxy_enable))
        for name, value in (
            ("ProxyServer", state.proxy_server),
            ("ProxyOverride", state.proxy_override),
            ("AutoConfigURL", state.auto_config_url),
        ):
            if value is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))
        if state.auto_detect is None:
            try:
                winreg.DeleteValue(key, "AutoDetect")
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, "AutoDetect", 0, winreg.REG_DWORD, int(state.auto_detect))
    _notify_windows()


class ProxySession:
    def __init__(self, backup_path: Path) -> None:
        self.backup_path = backup_path
        self.original: ProxyState | None = None

    @property
    def has_backup(self) -> bool:
        return self.backup_path.exists()

    def recover_if_needed(self) -> bool:
        if not self.backup_path.exists():
            return False
        data = json.loads(self.backup_path.read_text(encoding="utf-8"))
        write_proxy_state(ProxyState.from_dict(data))
        self.backup_path.unlink(missing_ok=True)
        return True

    def activate(self, host: str, port: int) -> ProxyState:
        if self.original is None:
            self.original = read_proxy_state()
            self.backup_path.parent.mkdir(parents=True, exist_ok=True)
            self.backup_path.write_text(
                json.dumps(asdict(self.original), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        active = ProxyState(
            proxy_enable=1,
            proxy_server=f"{host}:{port}",
            proxy_override=self.original.proxy_override,
            auto_config_url=None,
            auto_detect=0,
        )
        write_proxy_state(active)
        return active

    def restore(self) -> bool:
        state = self.original
        if state is None and self.backup_path.exists():
            state = ProxyState.from_dict(
                json.loads(self.backup_path.read_text(encoding="utf-8"))
            )
        if state is None:
            return False
        write_proxy_state(state)
        self.backup_path.unlink(missing_ok=True)
        self.original = None
        return True
