from __future__ import annotations

import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from urllib.parse import urlsplit

from main import (
    BASE_DIR,
    ConfigError,
    build_command,
    can_connect,
    find_mitmdump,
    load_config,
)
from windows_proxy import ProxySession, read_proxy_state

BACKUP_PATH = BASE_DIR / "logs" / "proxy_backup.json"


class RenameDialog(simpledialog.Dialog):
    def __init__(self, parent: tk.Misc, initialvalue: str) -> None:
        self.initialvalue = initialvalue
        self.result: str | None = None
        super().__init__(parent, title="Rename video")

    def body(self, master: tk.Misc) -> tk.Entry:
        ttk.Label(master, text="New file name:").grid(row=0, column=0, sticky="w")
        self.entry = ttk.Entry(master, width=48)
        self.entry.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.entry.insert(0, self.initialvalue)
        extension_length = len(Path(self.initialvalue).suffix)
        stem_length = max(0, len(self.initialvalue) - extension_length)
        self.entry.selection_range(0, stem_length)
        self.entry.icursor(stem_length)
        return self.entry

    def apply(self) -> None:
        self.result = self.entry.get()


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("WechatVideoSniffer")
        self.root.geometry("860x650")
        self.root.minsize(720, 520)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.config_path = BASE_DIR / "config.json"
        self.config = load_config(self.config_path)
        self.proxy = ProxySession(BACKUP_PATH)
        self.process: subprocess.Popen[str] | None = None
        self.messages: queue.Queue[str] = queue.Queue()
        output = Path(self.config.get("output_dir", "output"))
        if not output.is_absolute():
            output = BASE_DIR / output
        self.address = tk.StringVar()
        self.output = tk.StringVar(value=str(output))
        self.status = tk.StringVar(value="Not monitoring")
        self.proxy_status = tk.StringVar(value="Windows proxy: reading...")
        self.download_status = tk.StringVar(value="No download task")
        self.download_items: dict[str, str] = {}
        self.download_names: dict[str, str] = {}
        self.download_paths: dict[str, str] = {}
        self.download_urls: dict[str, str] = {}
        self.download_kinds: dict[str, str] = {}
        self.download_headers: dict[str, dict[str, str]] = {}
        self.download_state_path = BASE_DIR / "logs" / "download_list.json"
        self.pending_renames: dict[str, str] = {}
        self.redownload_rows: dict[str, str] = {}
        self.build_ui()
        self.load_download_list()
        self.recover_stale_proxy()
        self.root.after(100, self.poll)
        self.refresh_proxy_status()

    def build_ui(self) -> None:
        menubar = tk.Menu(self.root)
        settings_menu = tk.Menu(menubar, tearoff=False)
        settings_menu.add_command(label="Preferences...", command=self.open_settings)
        menubar.add_cascade(label="Settings", menu=settings_menu)
        self.root.config(menu=menubar)

        box = ttk.Frame(self.root, padding=14)
        box.pack(fill="both", expand=True)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(6, weight=1)
        ttk.Label(box, text="WechatVideoSniffer", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="w")

        state = ttk.LabelFrame(box, text="Monitoring", padding=10)
        state.grid(row=1, column=0, sticky="ew", pady=8)
        state.columnconfigure(1, weight=1)
        ttk.Label(state, text="Status:").grid(row=0, column=0, sticky="w")
        ttk.Label(state, textvariable=self.status).grid(row=0, column=1, sticky="w")
        ttk.Label(state, textvariable=self.proxy_status).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
        buttons = ttk.Frame(state)
        buttons.grid(row=0, column=2, rowspan=2, sticky="e")
        self.start_button = ttk.Button(buttons, text="Start monitoring", command=self.start)
        self.start_button.pack(side="left", padx=4)
        self.stop_button = ttk.Button(buttons, text="Stop monitoring", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=4)

        address = ttk.LabelFrame(box, text="Video or playback page URL", padding=10)
        address.grid(row=2, column=0, sticky="ew", pady=8)
        address.columnconfigure(0, weight=1)
        ttk.Entry(address, textvariable=self.address).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(address, text="Paste", command=self.paste).grid(row=0, column=1, padx=3)
        ttk.Button(address, text="Download video", command=self.download).grid(row=0, column=2, padx=3)
        ttk.Label(address, text="Supports MP4/M3U8, web pages, and WeChat mini-program codes.", foreground="#666").grid(row=1, column=0, columnspan=3, sticky="w", pady=(7, 0))

        output = ttk.LabelFrame(box, text="Save location", padding=10)
        output.grid(row=3, column=0, sticky="ew", pady=8)
        output.columnconfigure(0, weight=1)
        ttk.Entry(output, textvariable=self.output).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(output, text="Browse...", command=self.choose_output).grid(row=0, column=1, padx=3)
        ttk.Button(output, text="Open folder", command=self.open_output).grid(row=0, column=2, padx=3)
        ttk.Label(output, textvariable=self.download_status).grid(row=1, column=0, columnspan=3, sticky="w", pady=(7, 0))

        list_header = ttk.Frame(box)
        list_header.grid(row=4, column=0, sticky="ew", pady=(8, 3))
        list_header.columnconfigure(0, weight=1)
        ttk.Label(list_header, text="Download list").grid(row=0, column=0, sticky="w")
        ttk.Button(list_header, text="Rename selected", command=self.rename_selected).grid(row=0, column=1, sticky="e", padx=(0, 6))
        ttk.Button(list_header, text="Continue / retry", command=self.download_again).grid(row=0, column=2, sticky="e", padx=(0, 6))
        ttk.Button(list_header, text="Open path", command=self.open_selected_path).grid(row=0, column=3, sticky="e", padx=(0, 6))
        ttk.Button(list_header, text="Clear list", command=self.clear_download_list).grid(row=0, column=4, sticky="e")
        list_frame = ttk.Frame(box)
        list_frame.grid(row=5, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.download_list = ttk.Treeview(list_frame, columns=("name", "path", "status"), show="headings", height=6)
        self.download_list.heading("name", text="Video")
        self.download_list.heading("path", text="Full path")
        self.download_list.heading("status", text="Status")
        self.download_list.column("name", width=180, anchor="w")
        self.download_list.column("path", width=420, anchor="w")
        self.download_list.column("status", width=150, anchor="w")
        self.download_list.tag_configure("missing", foreground="#777777", font=("Segoe UI", 9, "overstrike"))
        self.download_list.grid(row=0, column=0, sticky="nsew")
        list_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.download_list.yview)
        list_scroll.grid(row=0, column=1, sticky="ns")
        self.download_list.configure(yscrollcommand=list_scroll.set)
        self.download_menu = tk.Menu(self.root, tearoff=False)
        self.download_menu.add_command(label="Open file path", command=self.open_selected_path)
        self.download_menu.add_command(label="Rename", command=self.rename_selected)
        self.download_menu.add_command(label="Continue / retry", command=self.download_again)
        self.download_list.bind("<Button-3>", self.show_download_menu)

        ttk.Label(box, text="Live log").grid(row=6, column=0, sticky="w", pady=(8, 3))
        self.log = scrolledtext.ScrolledText(box, state="disabled", font=("Consolas", 9), wrap="word")
        self.log.grid(row=7, column=0, sticky="nsew")
        box.rowconfigure(7, weight=1)

    def open_settings(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Preferences")
        window.transient(self.root)
        window.resizable(False, False)
        window.grab_set()

        settings = ttk.Frame(window, padding=14)
        settings.grid(sticky="nsew")
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)

        listen_host = tk.StringVar(value=str(self.config.get("listen_host", "127.0.0.1")))
        listen_port = tk.StringVar(value=str(self.config.get("listen_port", 8888)))
        upstream = self.config.get("upstream_proxy", {})
        proxy_enabled = tk.BooleanVar(value=bool(upstream.get("enabled", False)))
        proxy_type = tk.StringVar(value=str(upstream.get("type", "http")))
        proxy_host = tk.StringVar(value=str(upstream.get("host", "127.0.0.1")))
        proxy_port = tk.StringVar(value=str(upstream.get("port", 10808)))
        ssl_insecure = tk.BooleanVar(value=bool(self.config.get("ssl_insecure", False)))
        auto_download = tk.BooleanVar(value=bool(self.config.get("auto_download", False)))
        auto_download_catalog = tk.BooleanVar(value=bool(self.config.get("auto_download_catalog", False)))
        output_dir = tk.StringVar(value=self.output.get())
        log_file = tk.StringVar(value=str(self.config.get("log_file", "logs/videos.log")))

        listener = ttk.LabelFrame(settings, text="Local listener", padding=10)
        listener.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        ttk.Label(listener, text="Address").grid(row=0, column=0, sticky="w")
        ttk.Entry(listener, textvariable=listen_host, width=22).grid(row=0, column=1, padx=(6, 14))
        ttk.Label(listener, text="Port").grid(row=0, column=2, sticky="w")
        ttk.Entry(listener, textvariable=listen_port, width=10).grid(row=0, column=3, padx=(6, 0))

        proxy_frame = ttk.LabelFrame(settings, text="Optional upstream proxy", padding=10)
        proxy_frame.grid(row=1, column=0, columnspan=4, sticky="ew", pady=8)
        proxy_frame.columnconfigure(1, weight=1)
        proxy_frame.columnconfigure(3, weight=1)
        ttk.Checkbutton(
            proxy_frame,
            text="Use an upstream proxy (leave unchecked for a direct connection)",
            variable=proxy_enabled,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(proxy_frame, text="Type").grid(row=1, column=0, sticky="w")
        type_box = ttk.Combobox(proxy_frame, textvariable=proxy_type, values=("http", "https", "socks5"), state="readonly", width=12)
        type_box.grid(row=1, column=1, sticky="w", padx=(6, 14))
        ttk.Label(proxy_frame, text="Host").grid(row=1, column=2, sticky="w")
        host_entry = ttk.Entry(proxy_frame, textvariable=proxy_host, width=22)
        host_entry.grid(row=1, column=3, sticky="ew", padx=(6, 0))
        ttk.Label(proxy_frame, text="Port").grid(row=2, column=0, sticky="w", pady=(7, 0))
        port_entry = ttk.Entry(proxy_frame, textvariable=proxy_port, width=12)
        port_entry.grid(row=2, column=1, sticky="w", padx=(6, 14), pady=(7, 0))
        proxy_fields = (type_box, host_entry, port_entry)

        def update_proxy_fields(*_args) -> None:
            state = "normal" if proxy_enabled.get() else "disabled"
            for field in proxy_fields:
                field.configure(state=state)

        proxy_enabled.trace_add("write", update_proxy_fields)
        update_proxy_fields()

        behavior = ttk.LabelFrame(settings, text="Behavior", padding=10)
        behavior.grid(row=2, column=0, columnspan=4, sticky="ew", pady=8)
        ttk.Checkbutton(behavior, text="Automatically download detected videos", variable=auto_download).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(behavior, text="Automatically download videos found in catalog responses", variable=auto_download_catalog).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Checkbutton(behavior, text="Skip upstream HTTPS certificate verification (insecure)", variable=ssl_insecure).grid(row=2, column=0, sticky="w", pady=(4, 0))

        paths = ttk.LabelFrame(settings, text="Files", padding=10)
        paths.grid(row=3, column=0, columnspan=4, sticky="ew", pady=8)
        paths.columnconfigure(1, weight=1)
        ttk.Label(paths, text="Save videos to").grid(row=0, column=0, sticky="w")
        ttk.Entry(paths, textvariable=output_dir, width=44).grid(row=0, column=1, sticky="ew", padx=6)

        def browse_output() -> None:
            selected = filedialog.askdirectory(parent=window, initialdir=output_dir.get())
            if selected:
                output_dir.set(selected)

        ttk.Button(paths, text="Browse...", command=browse_output).grid(row=0, column=2)
        ttk.Label(paths, text="Video URL log").grid(row=1, column=0, sticky="w", pady=(7, 0))
        ttk.Entry(paths, textvariable=log_file, width=44).grid(row=1, column=1, columnspan=2, sticky="ew", padx=6, pady=(7, 0))

        buttons = ttk.Frame(settings)
        buttons.grid(row=4, column=0, columnspan=4, sticky="e", pady=(10, 0))

        def save_preferences() -> None:
            try:
                host_value = listen_host.get().strip()
                port_value = int(listen_port.get().strip())
                proxy_host_value = proxy_host.get().strip()
                proxy_port_value = int(proxy_port.get().strip())
                proxy_type_value = proxy_type.get().strip() or "http"
                if not host_value:
                    raise ValueError("Local listener address cannot be empty.")
                if not 1 <= port_value <= 65535:
                    raise ValueError("Local listener port must be between 1 and 65535.")
                if proxy_enabled.get() and not proxy_host_value:
                    raise ValueError("Enter the upstream proxy host, or turn off upstream proxy.")
                if proxy_enabled.get() and proxy_type_value not in {"http", "https", "socks5"}:
                    raise ValueError("Choose HTTP, HTTPS, or SOCKS5 for the upstream proxy type.")
                if not 1 <= proxy_port_value <= 65535:
                    raise ValueError("Upstream proxy port must be between 1 and 65535.")

                self.config.update({
                    "listen_host": host_value,
                    "listen_port": port_value,
                    "upstream_proxy": {
                        "enabled": proxy_enabled.get(),
                        "type": proxy_type_value,
                        "host": proxy_host_value,
                        "port": proxy_port_value,
                    },
                    "ssl_insecure": ssl_insecure.get(),
                    "auto_download": auto_download.get(),
                    "auto_download_catalog": auto_download_catalog.get(),
                    "log_file": log_file.get().strip() or "logs/videos.log",
                })
                self.output.set(output_dir.get().strip() or "output")
                self.save_config()
                self.config = load_config(self.config_path)
            except (ConfigError, ValueError, OSError, RuntimeError) as exc:
                messagebox.showerror("Preferences", str(exc), parent=window)
                return
            if self.process is not None and self.process.poll() is None:
                messagebox.showinfo("Preferences", "Saved. Restart monitoring for these changes to take effect.", parent=window)
            window.destroy()

        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Save", command=save_preferences).pack(side="right")

    @staticmethod
    def _download_name(value: str) -> str:
        value = value.strip().split("\n", 1)[0].strip().strip('"')
        value = value.removeprefix("Previously completed:").strip()
        value = value.removeprefix("Already exists:").strip()
        value = value.removeprefix("Already downloaded:").strip()
        name = Path(value).name
        if name.endswith(".part"):
            name = name[:-5]
        return name or value

    def _apply_pending_rename(self, key: str, item: str, values: tuple) -> tuple:
        new_name = self.pending_renames.pop(key, "")
        if not new_name:
            return values
        path_text = self.download_paths.get(key) or (str(values[1]) if len(values) > 1 else "")
        old_path = Path(path_text) if path_text else None
        if old_path is None or not old_path.is_file():
            self.download_list.item(item, values=(new_name, path_text, "Completed (rename pending)"))
            return (new_name, path_text, "Completed (rename pending)")
        new_path = old_path.with_name(new_name)
        try:
            old_path.rename(new_path)
            history_path = BASE_DIR / "logs" / "downloaded.json"
            history = json.loads(history_path.read_text(encoding="utf-8"))
            history.setdefault("completed_files", {})[key] = str(new_path.resolve())
            history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
            self.download_names[key] = new_path.name
            self.download_paths[key] = str(new_path.resolve())
            return (new_path.name, str(new_path.resolve()), "Completed")
        except (OSError, json.JSONDecodeError) as exc:
            self.log_line(f"[GUI] Deferred rename failed: {exc}")
            return (new_name, path_text, "Completed (rename failed)")

    def save_download_list(self) -> None:
        records = []
        for key, item in self.download_items.items():
            values = tuple(self.download_list.item(item, "values"))
            if len(values) < 3:
                continue
            records.append({
                "key": key,
                "name": str(values[0]),
                "path": self.download_paths.get(key, str(values[1])),
                "status": str(values[2]),
                "url": self.download_urls.get(key, ""),
                "kind": self.download_kinds.get(key, "download"),
                "headers": self.download_headers.get(key, {}),
            })
        self.download_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.download_state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.download_state_path)

    def load_download_list(self) -> None:
        records = None
        if self.download_state_path.exists():
            try:
                records = json.loads(self.download_state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                records = []
        else:
            # Migrate the old completed-download history into the visible list.
            history_path = BASE_DIR / "logs" / "downloaded.json"
            try:
                history = json.loads(history_path.read_text(encoding="utf-8"))
                records = [
                    {"key": key, "path": path, "name": Path(path).name, "status": "Completed", "url": ""}
                    for key, path in history.get("completed_files", {}).items()
                ]
            except (OSError, json.JSONDecodeError, AttributeError):
                records = []
        if not isinstance(records, list):
            records = []
        restored = 0
        for record in records:
            if not isinstance(record, dict):
                continue
            key = str(record.get("key", ""))
            path = str(record.get("path", ""))
            name = str(record.get("name") or (Path(path).name if path else key))
            status = str(record.get("status", "Interrupted"))
            url = str(record.get("url", ""))
            kind = str(record.get("kind", "hls" if urlsplit(url).path.lower().endswith(".m3u8") else "download"))
            headers = record.get("headers", {})
            if not isinstance(headers, dict):
                headers = {}
            if not key:
                continue
            if status.lower().startswith(("queued", "downloading")):
                has_partial = bool(path) and Path(path + ".part").is_file()
                if kind == "hls" and has_partial:
                    status = "Interrupted (HLS will restart)"
                else:
                    status = "Interrupted (partial saved)" if has_partial else "Interrupted"
            elif status == "Completed" and path and not Path(path).is_file():
                status = "File missing"
            item = self.download_list.insert("", "end", values=(name, path, status))
            self.download_items[key] = item
            self.download_names[key] = name
            self.download_paths[key] = path
            self.download_urls[key] = url
            self.download_kinds[key] = kind
            self.download_headers[key] = {str(k): str(v) for k, v in headers.items()}
            restored += 1
        self.save_download_list()
        if restored:
            self.download_status.set(f"Restored {restored} download records")

    def mark_downloads_interrupted(self) -> None:
        for key, item in self.download_items.items():
            values = tuple(self.download_list.item(item, "values"))
            if len(values) < 3 or not str(values[2]).lower().startswith(("queued", "downloading")):
                continue
            path = self.download_paths.get(key, "")
            has_partial = bool(path) and Path(path + ".part").is_file()
            kind = self.download_kinds.get(key, "download")
            if kind == "hls" and has_partial:
                status = "Interrupted (HLS will restart)"
            else:
                status = "Interrupted (partial saved)" if has_partial else "Interrupted"
            self.download_list.item(item, values=(values[0], values[1], status))
        self.save_download_list()

    def update_download_list(self, value: str) -> None:
        text = value.strip()
        if not text:
            return
        if "[HLS TASK] " in text or "[DOWNLOAD TASK] " in text:
            prefix = "[HLS TASK] " if "[HLS TASK] " in text else "[DOWNLOAD TASK] "
            try:
                event = json.loads(text.split(prefix, 1)[1])
                key = str(event.get("key", ""))
                path = str(event.get("path", ""))
                status = str(event.get("status", ""))
                url = str(event.get("url", ""))
                kind = str(event.get("kind", "hls" if prefix.startswith("[HLS") else "download"))
                headers = event.get("headers", {})
                if not isinstance(headers, dict):
                    headers = {}
                if not key:
                    return
                reuse_key = self.redownload_rows.pop(key, None)
                if reuse_key and reuse_key != key:
                    old_item = self.download_items.pop(reuse_key, None)
                    if old_item:
                        self.download_items[key] = old_item
                    self.download_names[key] = self.download_names.pop(reuse_key, Path(path).name if path else "Unknown video")
                    self.download_paths.pop(reuse_key, None)
                    self.download_urls.pop(reuse_key, None)
                    self.download_kinds.pop(reuse_key, None)
                    self.download_headers.pop(reuse_key, None)
                name = self.download_names.get(key, Path(path).name if path else "Unknown video")
                item = self.download_items.get(key)
                if item is None:
                    item = self.download_list.insert("", "end", values=(name, path, status))
                    self.download_items[key] = item
                else:
                    self.download_list.item(item, values=(name, path, status), tags=())
                self.download_names[key] = name
                self.download_paths[key] = path
                self.download_urls[key] = url
                self.download_kinds[key] = kind
                self.download_headers[key] = {str(k): str(v) for k, v in headers.items()}
                if status == "Completed":
                    current = tuple(self.download_list.item(item, "values"))
                    updated = self._apply_pending_rename(key, item, current)
                    self.download_list.item(item, values=updated, tags=())
                self.download_list.see(item)
                self.save_download_list()
            except (json.JSONDecodeError, AttributeError, TypeError, OSError):
                pass
            return
        if "[HLS HISTORY] " in text:
            try:
                event = json.loads(text.split("[HLS HISTORY] ", 1)[1])
                key = str(event.get("key", ""))
                path = str(event.get("path", ""))
                exists = bool(path) and Path(path).is_file()
                name = Path(path).name if path else "Unknown downloaded file"
                item = self.download_items.get(key)
                values = (name, path or "Recorded file path unavailable", "Already downloaded" if exists else "File missing")
                tags = () if exists else ("missing",)
                if item is None:
                    item = self.download_list.insert("", "end", values=values, tags=tags)
                    self.download_items[key] = item
                else:
                    self.download_list.item(item, values=values, tags=tags)
                self.download_names[key] = name
                self.download_paths[key] = path
                self.download_urls[key] = str(event.get("url", ""))
                self.download_kinds[key] = "hls"
                self.download_headers.setdefault(key, {})
                self.download_list.see(item)
                self.save_download_list()
            except (json.JSONDecodeError, AttributeError, TypeError, OSError):
                pass
            return
        # Older status-only lines lack a stable task key; structured task events
        # above carry the URL and target needed to restore a task after restart.

    def clear_download_list(self) -> None:
        for item in self.download_list.get_children():
            self.download_list.delete(item)
        self.download_items.clear()
        self.download_names.clear()
        self.download_paths.clear()
        self.download_urls.clear()
        self.download_kinds.clear()
        self.download_headers.clear()
        self.pending_renames.clear()
        self.redownload_rows.clear()
        self.save_download_list()
        self.download_status.set("Download list cleared")

    def show_download_menu(self, event: tk.Event) -> None:
        item = self.download_list.identify_row(event.y)
        if not item:
            return
        self.download_list.selection_set(item)
        self.download_list.focus(item)
        try:
            self.download_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.download_menu.grab_release()

    def _selected_key(self) -> tuple[str, str] | None:
        selection = self.download_list.selection()
        if not selection:
            return None
        item = selection[0]
        for key, mapped in self.download_items.items():
            if mapped == item:
                return key, item
        return None

    def rename_selected(self) -> None:
        selected = self._selected_key()
        if selected is None:
            messagebox.showinfo("Rename", "Select a download first.")
            return
        key, item = selected
        values = self.download_list.item(item, "values")
        path_text = self.download_paths.get(key) or (str(values[1]) if len(values) > 1 else "")
        old_path = Path(path_text) if path_text else None
        values = tuple(self.download_list.item(item, "values"))
        status = str(values[2]) if len(values) > 2 else ""
        is_downloading = status.startswith("Downloading") or status == "Queued"
        if is_downloading:
            initial_name = str(values[0]) if values else "video.mp4"
        else:
            if old_path is None or not old_path.is_file():
                messagebox.showerror("Rename failed", "The recorded file no longer exists. Download it again first.")
                return
            initial_name = old_path.name
        new_name = RenameDialog(self.root, initial_name).result
        if not new_name:
            return
        new_name = re.sub(r"[\\/:*?\"<>|]", "_", new_name).strip(" .")
        if not Path(new_name).suffix:
            new_name += (old_path.suffix if old_path else ".mp4")
        if is_downloading:
            self.pending_renames[key] = new_name
            self.download_names[key] = new_name
            self.download_list.item(item, values=(new_name, path_text, "Downloading (rename pending)"))
            self.log_line(f"[GUI] Rename scheduled after download: {new_name}")
            self.save_download_list()
            return
        new_path = old_path.with_name(new_name)
        try:
            old_path.rename(new_path)
            history_path = BASE_DIR / "logs" / "downloaded.json"
            history = json.loads(history_path.read_text(encoding="utf-8"))
            history.setdefault("completed_files", {})[key] = str(new_path.resolve())
            history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("Rename failed", str(exc))
            return
        self.download_names[key] = new_path.name
        self.download_paths[key] = str(new_path.resolve())
        status = str(values[2]) if len(values) > 2 else "Completed"
        self.download_list.item(item, values=(new_path.name, str(new_path.resolve()), status), tags=())
        self.save_download_list()

    def open_selected_path(self) -> None:
        selected = self._selected_key()
        if selected is None:
            messagebox.showinfo("Open path", "Select a download first.")
            return
        key, item = selected
        values = self.download_list.item(item, "values")
        path_text = self.download_paths.get(key) or (str(values[1]) if len(values) > 1 else "")
        if not path_text:
            messagebox.showerror("Open path", "No recorded file path is available.")
            return
        path = Path(path_text)
        try:
            if path.is_file():
                # Windows Explorer opens the containing folder and selects the file.
                subprocess.Popen(["explorer.exe", "/select,", str(path)])
            else:
                folder = path if path.is_dir() else path.parent
                if folder.exists():
                    os.startfile(str(folder))
                else:
                    messagebox.showwarning("Open path", f"The recorded path no longer exists:\n{path}")
        except OSError as exc:
            messagebox.showerror("Open path failed", str(exc))

    def download_again(self) -> None:
        selected = self._selected_key()
        if selected is None:
            messagebox.showinfo("Continue download", "Select an interrupted, failed, or missing download first.")
            return
        key, item = selected
        values = tuple(self.download_list.item(item, "values"))
        status = str(values[2]) if len(values) > 2 else ""
        path = self.download_paths.get(key, str(values[1]) if len(values) > 1 else "")
        if status == "Completed" and path and Path(path).is_file():
            messagebox.showinfo("Continue download", "This file is already complete.")
            return
        url = self.download_urls.get(key, "")
        if not url:
            messagebox.showerror("Continue download", "Play the video again to obtain a current URL, then retry.")
            return
        if not self.start():
            return
        request_path = BASE_DIR / "logs" / "redownload.jsonl"
        kind = self.download_kinds.get(key, "hls" if urlsplit(url).path.lower().endswith(".m3u8") else "download")
        request = {
            "url": url,
            "key": key,
            "path": path,
            "kind": kind,
            "headers": self.download_headers.get(key, {}),
        }
        self.redownload_rows[key] = key
        with request_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.download_list.item(item, values=(values[0], path, "Queued"), tags=())
        self.save_download_list()
        self.log_line(f"[GUI] Continue requested for: {values[0]}")

    def log_line(self, value: str) -> None:
        self.update_download_list(value)
        self.log.configure(state="normal")
        self.log.insert("end", value.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        if "[DOWNLOAD TASK] " in value or "[HLS TASK] " in value:
            try:
                marker, payload = value.split("] ", 1)
                event = json.loads(payload)
                path = Path(str(event.get("path", "")))
                self.download_status.set(f"{event.get('status', 'Download')}: {path.name}")
            except (ValueError, json.JSONDecodeError, TypeError):
                self.download_status.set(value.strip())
        elif "[DOWNLOAD" in value or "[HLS" in value:
            self.download_status.set(value.strip())

    def poll(self) -> None:
        try:
            while True:
                self.log_line(self.messages.get_nowait())
        except queue.Empty:
            pass
        if self.process is not None and self.process.poll() is not None:
            code = self.process.returncode
            self.process = None
            try:
                self.proxy.restore()
            except Exception as exc:
                self.log_line(f"[GUI] Proxy restore failed: {exc}")
            self.mark_downloads_interrupted()
            self.status.set(f"Process exited ({code}); proxy restored")
            self.set_running(False)
            self.refresh_proxy_status()
        self.root.after(100, self.poll)

    def recover_stale_proxy(self) -> None:
        if self.proxy.has_backup:
            try:
                if self.proxy.recover_if_needed():
                    self.log_line("[GUI] Recovered proxy settings from previous run")
            except Exception as exc:
                messagebox.showerror("Proxy recovery failed", str(exc))

    def refresh_proxy_status(self) -> None:
        try:
            state = read_proxy_state()
            self.proxy_status.set("Windows proxy: " + (state.proxy_server if state.proxy_enable else "disabled"))
        except Exception as exc:
            self.proxy_status.set(f"Windows proxy read failed: {exc}")

    def save_config(self) -> Path:
        path = Path(self.output.get().strip()).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        self.config["output_dir"] = str(path)
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def choose_output(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output.get())
        if selected:
            self.output.set(selected)
            self.save_config()

    def open_output(self) -> None:
        os.startfile(self.save_config())

    def paste(self) -> None:
        try:
            self.address.set(self.root.clipboard_get().strip())
        except tk.TclError:
            messagebox.showwarning("Clipboard", "Clipboard is empty.")

    @staticmethod
    def listening(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            return False

    def start(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            return True
        try:
            self.save_config()
            executable = find_mitmdump()
            if not executable:
                raise RuntimeError("mitmdump was not found. Install requirements.txt first.")
            upstream = self.config.get("upstream_proxy", {})
            runtime_config = dict(self.config)
            if upstream.get("enabled") and not can_connect(upstream["host"], upstream["port"]):
                runtime_config["upstream_proxy"] = {**upstream, "enabled": False}
                self.log_line("[GUI] v2rayN unavailable; using direct mode")
            elif upstream.get("enabled"):
                self.log_line("[GUI] Using v2rayN upstream")
            else:
                self.log_line("[GUI] Using direct connection (no upstream proxy)")
            host = self.config.get("listen_host", "127.0.0.1")
            port = int(self.config.get("listen_port", 8888))
            if self.listening(host, port):
                raise RuntimeError(f"Port already in use: {host}:{port}")
            env = os.environ.copy()
            env["WVS_CONFIG"] = str(self.config_path)
            env["WVS_CONFIG_INLINE"] = json.dumps(runtime_config, ensure_ascii=False)
            env["WVS_DEBUG"] = "1"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.process = subprocess.Popen(build_command(runtime_config, True, executable), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, env=env, creationflags=flags)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and not self.listening(host, port):
                if self.process.poll() is not None:
                    raise RuntimeError("mitmdump failed to start")
                time.sleep(0.1)
            if not self.listening(host, port):
                raise RuntimeError("mitmdump start timed out")
            self.proxy.activate(host, port)
            threading.Thread(target=self.read_process, daemon=True).start()
            self.status.set("Monitoring")
            self.set_running(True)
            self.refresh_proxy_status()
            self.log_line(f"[GUI] Monitoring started; Windows proxy is {host}:{port}")
            return True
        except Exception as exc:
            if self.process is not None:
                self.process.kill()
                self.process = None
            try:
                self.proxy.restore()
            except Exception:
                pass
            self.set_running(False)
            self.refresh_proxy_status()
            messagebox.showerror("Start failed", str(exc))
            return False

    def read_process(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        for line in self.process.stdout:
            self.messages.put(line)

    def set_running(self, value: bool) -> None:
        self.start_button.configure(state="disabled" if value else "normal")
        self.stop_button.configure(state="normal" if value else "disabled")

    def stop(self) -> None:
        try:
            self.proxy.restore()
        except Exception as exc:
            messagebox.showerror("Proxy restore failed", str(exc))
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        try:
            while True:
                self.log_line(self.messages.get_nowait())
        except queue.Empty:
            pass
        self.mark_downloads_interrupted()
        self.status.set("Stopped; proxy restored")
        self.set_running(False)
        self.refresh_proxy_status()

    def download(self) -> None:
        address = self.address.get().strip()
        if not address:
            messagebox.showwarning("Address", "Paste a video or playback page URL first.")
            return
        if not self.start():
            return
        if address.startswith("#"):
            self.root.clipboard_clear()
            self.root.clipboard_append(address)
            messagebox.showinfo("Open in WeChat", "The mini-program code was copied. Open it in WeChat and play the video.")
            return
        parsed = urlsplit(address)
        if parsed.scheme not in {"http", "https"}:
            messagebox.showerror("Address", "Use an http:// or https:// URL, or a WeChat mini-program code.")
            return
        webbrowser.open(address)
        self.log_line("[GUI] Opened address; play the video to trigger capture")

    def close(self) -> None:
        self.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    # Use a Windows font with full CJK glyph coverage in dialogs and entries.
    for font_name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        try:
            tkfont.nametofont(font_name, root=root).configure(family="Microsoft YaHei UI", weight="normal")
        except tk.TclError:
            pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()



