from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from mitmproxy import http

from downloader import VideoDownloader, find_video_urls

BASE_DIR = Path(__file__).resolve().parent
VIDEO_EXTENSIONS = {".mp4", ".m3u8", ".m4s", ".ts", ".webm", ".mov"}
PLAYLIST_CONTENT_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
}
SEGMENT_CONTENT_TYPES = {"video/mp2t"}


def normalized_content_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def url_extension(url: str) -> str:
    return Path(urlsplit(url).path).suffix.lower()


def classify_video(url: str, content_type: str) -> tuple[str, bool] | None:
    extension = url_extension(url)
    mime = normalized_content_type(content_type)

    if extension == ".m3u8" or mime in PLAYLIST_CONTENT_TYPES:
        return "M3U8", False
    if extension == ".ts" or mime in SEGMENT_CONTENT_TYPES:
        return "TS", True
    if extension in VIDEO_EXTENSIONS:
        return extension[1:].upper(), extension in {".m4s"}
    if mime.startswith("video/"):
        subtype = mime.removeprefix("video/").split("+", 1)[0]
        return (subtype or "VIDEO").upper(), False
    return None


class VideoSniffer:
    def __init__(self) -> None:
        self.seen_urls: set[str] = set()
        self.current_live_title: str | None = None
        self.lock = threading.Lock()
        self.debug = os.environ.get("WVS_DEBUG") == "1"
        config_path = Path(os.environ.get("WVS_CONFIG", BASE_DIR / "config.json"))
        self.config = self._load_config(config_path)
        configured_log = Path(self.config.get("log_file", "logs/videos.log"))
        self.log_path = configured_log if configured_log.is_absolute() else BASE_DIR / configured_log
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.api_log_path = BASE_DIR / "logs" / "course_api.jsonl"
        self.downloader = VideoDownloader(self.config, BASE_DIR)
        self.redownload_path = BASE_DIR / "logs" / "redownload.jsonl"
        self.redownload_path.unlink(missing_ok=True)
        threading.Thread(target=self._watch_redownloads, daemon=True).start()
        self._load_saved_course_names()

    def _watch_redownloads(self) -> None:
        while True:
            try:
                if self.redownload_path.exists():
                    lines = self.redownload_path.read_text(encoding="utf-8").splitlines()
                    self.redownload_path.unlink(missing_ok=True)
                    for line in lines:
                        try:
                            request = json.loads(line)
                            url = request.get("url")
                            if isinstance(url, str) and url.startswith(("http://", "https://")):
                                print(f"[HLS REDOWNLOAD] {url}", flush=True)
                                self.downloader.submit_hls(url, force=True)
                        except (json.JSONDecodeError, AttributeError):
                            continue
            except OSError as exc:
                print(f"[REDOWNLOAD WARNING] {exc}", flush=True)
            time.sleep(0.5)

    def _load_saved_course_names(self) -> None:
        if not self.api_log_path.exists():
            return
        try:
            with self.api_log_path.open("r", encoding="utf-8") as api_log:
                for line in api_log:
                    try:
                        record = json.loads(line)
                        # Restore title mappings only. Do not replay old catalog
                        # downloads every time the GUI starts.
                        bulk_download = self.downloader.bulk_download
                        self.downloader.bulk_download = False
                        try:
                            self.downloader.register_courses(record.get("response"))
                        finally:
                            self.downloader.bulk_download = bulk_download
                    except (json.JSONDecodeError, AttributeError):
                        continue
        except OSError as exc:
            print(f"[WARNING] Could not load saved course names: {exc}")
    @staticmethod
    def _load_config(path: Path) -> dict:
        try:
            with path.open("r", encoding="utf-8-sig") as file:
                return json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}

    def request(self, flow: http.HTTPFlow) -> None:
        if self.debug:
            print(f"[HTTP] {flow.request.method} {flow.request.pretty_url}")

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Detect videos as soon as headers arrive and stream their bodies."""
        if flow.response is None:
            return
        classification = classify_video(
            flow.request.pretty_url,
            flow.response.headers.get("content-type", ""),
        )
        if classification is None:
            return

        # Without streaming, mitmproxy buffers the complete response before forwarding it.
        # Large MP4 files can therefore appear frozen in the client.
        flow.response.stream = True
        self._record_video(flow, classification)

    def response(self, flow: http.HTTPFlow) -> None:
        # Kept as a fallback for unusual flows where responseheaders is not invoked.
        if flow.response is None:
            return
        classification = classify_video(
            flow.request.pretty_url,
            flow.response.headers.get("content-type", ""),
        )
        if classification is not None:
            self._record_video(flow, classification)
        self._discover_urls_in_json(flow)

    def _discover_urls_in_json(self, flow: http.HTTPFlow) -> None:
        content_type = normalized_content_type(flow.response.headers.get("content-type", ""))
        if "json" not in content_type or not flow.response.content:
            return
        try:
            payload = json.loads(flow.response.get_text(strict=False))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if any(
            name in flow.request.path
            for name in (
                "getVideoPage",
                "getVideoDetail",
                "getVideoPkgDetail",
                "getLivepkg",
                "getLiveinfo",
            )
        ):
            record = {
                "url": flow.request.pretty_url,
                "request_body": flow.request.get_text(strict=False),
                "response": payload,
            }
            with self.api_log_path.open("a", encoding="utf-8") as api_log:
                api_log.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.downloader.register_courses(payload)
        if "getLiveinfoExt" in flow.request.path:
            rows = payload.get("rows") if isinstance(payload, dict) else None
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                title = rows[0].get("live_title") or rows[0].get("title")
                if isinstance(title, str) and title.strip():
                    self.current_live_title = title.strip()
        for url in sorted(find_video_urls(payload)):
            print(f"[VIDEO URL IN API] {url}", flush=True)
            self.downloader.submit(url, dict(flow.request.headers))

    def _record_video(
        self, flow: http.HTTPFlow, classification: tuple[str, bool]
    ) -> None:
        url = flow.request.pretty_url
        content_type = flow.response.headers.get("content-type", "")
        video_type, is_segment = classification
        if is_segment and not self.debug:
            return

        with self.lock:
            if url in self.seen_urls:
                return
            self.seen_urls.add(url)

        if is_segment:
            print(f"[VIDEO SEGMENT] {url}")
            return

        content_length = flow.response.headers.get("content-length", "Unknown")
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        block = (
            "=" * 60
            + "\n\n[VIDEO FOUND]\n\n"
            + f"Time:\n{timestamp}\n\n"
            + f"Type:\n{video_type}\n\n"
            + f"Method:\n{flow.request.method}\n\n"
            + f"URL:\n{url}\n\n"
            + f"Content-Type:\n{content_type or 'Unknown'}\n\n"
            + f"Content-Length:\n{content_length}\n\n"
            + "=" * 60
            + "\n"
        )
        print("\n" + block, flush=True)
        if video_type == "M3U8":
            self.downloader.submit_hls(
                url, dict(flow.request.headers), self.current_live_title
            )
        else:
            self.downloader.submit(url, dict(flow.request.headers))
        try:
            with self.log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(block + "\n")
        except OSError as exc:
            print(f"[WARNING] Could not write log file: {exc}")


addons = [VideoSniffer()]
