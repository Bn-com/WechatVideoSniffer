from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

APP_NAME = "WechatVideoSniffer"
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"


class ConfigError(Exception):
    pass


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            config = json.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc

    host = config.get("listen_host", "127.0.0.1")
    port = config.get("listen_port", 8888)
    if not isinstance(host, str) or not host:
        raise ConfigError("listen_host must be a non-empty string")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("listen_port must be an integer from 1 to 65535")

    upstream = config.get("upstream_proxy", {})
    if not isinstance(upstream, dict):
        raise ConfigError("upstream_proxy must be an object")
    if upstream.get("enabled", False):
        upstream_host = upstream.get("host")
        upstream_port = upstream.get("port")
        upstream_type = str(upstream.get("type", "http")).lower()
        if not isinstance(upstream_host, str) or not upstream_host:
            raise ConfigError("upstream_proxy.host must be a non-empty string")
        if not isinstance(upstream_port, int) or not 1 <= upstream_port <= 65535:
            raise ConfigError("upstream_proxy.port must be an integer from 1 to 65535")
        if upstream_type not in {"http", "https", "socks5"}:
            raise ConfigError("upstream_proxy.type must be http, https, or socks5")

    return config


def can_connect(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def port_is_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
        return True
    except OSError:
        return False


def find_mitmdump() -> str | None:
    executable = shutil.which("mitmdump")
    if executable:
        return executable
    executable_dir = Path(sys.executable).parent
    name = "mitmdump.exe" if os.name == "nt" else "mitmdump"
    candidates = [
        executable_dir / name,
        executable_dir / ("Scripts" if os.name == "nt" else "bin") / name,
    ]
    return next((str(candidate) for candidate in candidates if candidate.exists()), None)


def build_command(config: dict[str, Any], debug: bool, executable: str) -> list[str]:
    command = [
        executable,
        "--listen-host",
        config.get("listen_host", "127.0.0.1"),
        "--listen-port",
        str(config.get("listen_port", 8888)),
        "--set",
        "termlog_verbosity=warn",
        "-s",
        str(BASE_DIR / "sniffer.py"),
    ]
    upstream = config.get("upstream_proxy", {})
    if upstream.get("enabled", False):
        proxy_type = str(upstream.get("type", "http")).lower()
        command.extend(
            ["--mode", f"upstream:{proxy_type}://{upstream['host']}:{upstream['port']}"]
        )
    if config.get("ssl_insecure", False):
        command.append("--ssl-insecure")
    return command


def print_error(message: str) -> None:
    print("\n[ERROR]\n")
    print(message)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture video URLs through mitmproxy.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--debug", action="store_true", help="show every HTTP request")
    args = parser.parse_args()

    try:
        config_path = args.config.resolve()
        config = load_config(config_path)
    except ConfigError as exc:
        print_error(str(exc))
        return 2

    executable = find_mitmdump()
    if not executable:
        print_error(
            "mitmproxy is not installed or mitmdump is not on PATH.\n\n"
            "Install it with:\npython -m pip install -r requirements.txt"
        )
        return 2

    listen_host = config.get("listen_host", "127.0.0.1")
    listen_port = config.get("listen_port", 8888)
    if not port_is_available(listen_host, listen_port):
        print_error(
            f"Listening port is already in use:\n\n{listen_host}:{listen_port}\n\n"
            "Stop the other program or change listen_port in config.json."
        )
        return 2

    upstream = config.get("upstream_proxy", {})
    if upstream.get("enabled", False):
        if not can_connect(upstream["host"], upstream["port"]):
            print_error(
                "Cannot connect to upstream proxy:\n\n"
                f"{upstream['host']}:{upstream['port']}\n\n"
                "Please check your v2rayN proxy type and port."
            )
            return 2
        upstream_text = f"{upstream['type']}://{upstream['host']}:{upstream['port']}"
    else:
        upstream_text = "Disabled (direct connection)"

    print(f"{APP_NAME}\n" + "-" * 40)
    print(f"\nListening:\n{listen_host}:{listen_port}")
    print(f"\nUpstream Proxy:\n{upstream_text}")
    print("\nWindows proxy settings are NOT changed by this program.")
    print("Configure test traffic to use the listening address manually.")
    print("\nWaiting for video requests...")
    print("\nOpen WeChat Mini Program and play a video.")
    print("\nPress Ctrl+C to stop.\n")

    env = os.environ.copy()
    env["WVS_CONFIG"] = str(config_path)
    env["WVS_DEBUG"] = "1" if args.debug else "0"

    try:
        completed = subprocess.run(build_command(config, args.debug, executable), env=env)
        return completed.returncode
    except KeyboardInterrupt:
        print("\nStopped. Windows proxy settings were not changed.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())