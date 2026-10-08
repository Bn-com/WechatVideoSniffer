from __future__ import annotations

import json
import os
import queue
import re
import ssl
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

import imageio_ffmpeg

VIDEO_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
SAFE_NAME_PATTERN = re.compile(r"[^\w.()\[\]\- ]+", re.UNICODE)
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def is_downloadable_video_url(url: str) -> bool:
    return Path(urlsplit(url).path).suffix.lower() in {".mp4", ".webm", ".mov"}


def find_video_urls(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for nested in value.values():
            found.update(find_video_urls(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(find_video_urls(nested))
    elif isinstance(value, str):
        value = value.replace("\\/", "/")
        candidates = [value] if value.lower().startswith(("http://", "https://")) else VIDEO_URL_PATTERN.findall(value)
        for candidate in candidates:
            candidate = candidate.replace("\\/", "/").rstrip(",;)]}")
            if is_downloadable_video_url(candidate):
                found.add(candidate)
    return found


def safe_name(value: str, fallback: str = "video") -> str:
    name = SAFE_NAME_PATTERN.sub("_", value).strip(" .")[:170] or fallback
    if name.upper() in WINDOWS_RESERVED:
        name = f"_{name}"
    return name


CHINESE_DIGITS = {
    "\u96f6": 0,
    "\u4e00": 1,
    "\u4e8c": 2,
    "\u4e09": 3,
    "\u56db": 4,
    "\u4e94": 5,
    "\u516d": 6,
    "\u4e03": 7,
    "\u516b": 8,
    "\u4e5d": 9,
}
COURSE_NUMBER_PATTERN = re.compile(
    r"^\u7b2c([\u96f6\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e]+)\u8282[\s._\u3001-]*"
)


def chinese_number(value: str) -> int | None:
    ten = "\u5341"
    if value == ten:
        return 10
    if ten in value:
        left, right = value.split(ten, 1)
        tens = CHINESE_DIGITS.get(left, 1) if left else 1
        ones = CHINESE_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return CHINESE_DIGITS.get(value)


def format_course_title(title: str) -> str:
    stripped = title.strip()
    match = COURSE_NUMBER_PATTERN.match(stripped)
    if not match:
        return stripped
    number = chinese_number(match.group(1))
    if number is None:
        return stripped
    remainder = stripped[match.end():].strip()
    return f"{number:02d}. {remainder}" if remainder else f"{number:02d}"

def timestamped_stem(stem: str) -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"{stem}_{timestamp}"


def timestamped_filename(stem: str, suffix: str) -> str:
    return f"{timestamped_stem(stem)}{suffix}"


def safe_filename(url: str, title: str | None = None) -> str:
    source = Path(urlsplit(url).path)
    suffix = source.suffix.lower() or ".mp4"
    if title:
        stem = safe_name(format_course_title(title))
    else:
        name = unquote(source.name) or f"video{suffix}"
        stem = safe_name(Path(name).stem)
        suffix = Path(name).suffix.lower() or suffix
    return timestamped_filename(stem, suffix)


def media_key(value: str) -> str:
    """Match a catalog ext_access_no and the CDN filename at any quality."""
    name = Path(urlsplit(value).path).stem if "://" in value else Path(value).stem
    return name.rsplit("_", 1)[0].lower()

def polyv_mp4_url(ext_access_no: str, quality: int = 2) -> str:
    """Build Polyv's standard MP4 CDN URL from an authorized catalog ID."""
    base = media_key(ext_access_no)
    if not base or len(base) < 10:
        raise ValueError("Invalid Polyv access number")
    return f"https://mpv.videocc.net/{base[:10]}/{base[-1]}/{base}_{quality}.mp4"


@dataclass(frozen=True)
class DownloadTask:
    url: str
    headers: dict[str, str]
    title: str | None = None
    target: Path | None = None
    lock_path: Path | None = None


class VideoDownloader:
    def __init__(self, config: dict, base_dir: Path) -> None:
        configured = Path(config.get("output_dir", "output"))
        self.output_dir = configured if configured.is_absolute() else base_dir / configured
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = base_dir / "logs" / "downloaded.json"
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.completed_keys, self.completed_files = self._load_history()
        self.enabled = bool(config.get("auto_download", False))
        self.ssl_insecure = bool(config.get("ssl_insecure", False))
        self.bulk_download = bool(config.get("auto_download_catalog", False))
        upstream = config.get("upstream_proxy", {})
        self.upstream_proxy_host = upstream.get("host", "127.0.0.1")
        self.upstream_proxy_port = upstream.get("port", 10808)
        self.tasks: queue.Queue[DownloadTask] = queue.Queue()
        self.hls_tasks: queue.Queue[DownloadTask] = queue.Queue()
        self.queued: set[str] = set()
        self.hls_queued: set[str] = set()
        self.titles: dict[str, str] = {}
        self.lock = threading.Lock()
        self.opener = self._make_opener(config)
        self.worker = threading.Thread(target=self._run, name="video-downloader", daemon=True)
        self.hls_worker = threading.Thread(target=self._run_hls, name="hls-downloader", daemon=True)
        if self.enabled:
            self.worker.start()
            self.hls_worker.start()

    def _load_history(self) -> tuple[set[str], dict[str, str]]:
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            keys = {str(key) for key in data.get("completed_media_keys", [])}
            files = {str(key): str(value) for key, value in data.get("completed_files", {}).items()}
            return keys, files
        except (OSError, json.JSONDecodeError, AttributeError):
            return set(), {}

    def mark_completed(self, key: str, filename: str | None = None) -> None:
        with self.lock:
            self.completed_keys.add(key)
            if filename:
                self.completed_files[key] = filename
            temp = self.history_path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(
                    {
                        "completed_media_keys": sorted(self.completed_keys),
                        "completed_files": self.completed_files,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temp.replace(self.history_path)
    def _make_opener(self, config: dict):
        handlers = []
        upstream = config.get("upstream_proxy", {})
        if upstream.get("enabled") and str(upstream.get("type", "http")).lower() in {"http", "https"}:
            proxy = f"http://{upstream['host']}:{upstream['port']}"
            handlers.append(ProxyHandler({"http": proxy, "https": proxy}))
        if self.ssl_insecure:
            handlers.append(HTTPSHandler(context=ssl._create_unverified_context()))
        return build_opener(*handlers)

    def register_courses(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        rows = payload.get("rows")
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            access_no = row.get("ext_access_no")
            title = row.get("video_name")
            if isinstance(access_no, str) and isinstance(title, str) and title.strip():
                key = media_key(access_no)
                clean_title = title.strip()
                self.titles[key] = clean_title
                self._rename_course_title_file(clean_title)
                self._rename_existing(key, clean_title)
                if self.bulk_download:
                    try:
                        self.submit(polyv_mp4_url(access_no), title=clean_title)
                    except ValueError as exc:
                        print(f"[CATALOG WARNING] {exc}: {access_no}", flush=True)

    def _rename_course_title_file(self, title: str) -> None:
        old_stem = safe_name(title)
        new_stem = safe_name(format_course_title(title))
        if old_stem == new_stem:
            return
        for suffix in (".mp4", ".webm", ".mov"):
            source = self.output_dir / f"{old_stem}{suffix}"
            target = self.output_dir / f"{new_stem}{suffix}"
            if source.exists() and not target.exists():
                source.rename(target)
                print(f"[RENAMED] {source.name} -> {target.name}", flush=True)


    def _rename_existing(self, key: str, title: str) -> None:
        for source in self.output_dir.iterdir():
            if not source.is_file() or source.suffix.lower() not in {".mp4", ".webm", ".mov"}:
                continue
            if media_key(source.name) != key:
                continue
            target = self.output_dir / (safe_name(format_course_title(title)) + source.suffix.lower())
            if source == target:
                return
            if target.exists():
                print(f"[RENAME SKIPPED] Target exists: {target}", flush=True)
                return
            source.rename(target)
            print(f"[RENAMED] {source.name} -> {target.name}", flush=True)
            return

    def submit(self, url: str, headers: dict[str, str] | None = None, title: str | None = None) -> None:
        if not self.enabled or not is_downloadable_video_url(url):
            return
        key = media_key(url)
        resolved_title = title or self.titles.get(key)
        if key in self.completed_keys:
            print(f"[DOWNLOAD SKIPPED] Previously completed: {resolved_title or key}", flush=True)
            return
        with self.lock:
            if url in self.queued:
                return
            self.queued.add(url)
        self.tasks.put(DownloadTask(url, headers or {}, resolved_title))
        print(f"[DOWNLOAD QUEUED] {url}", flush=True)

    def submit_hls(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        title: str | None = None,
        force: bool = False,
    ) -> None:
        if not self.enabled:
            return
        key = urlsplit(url).path
        if force:
            with self.lock:
                self.completed_keys.discard(key)
                self.hls_queued.discard(key)
        if key in self.completed_keys:
            stored = self.completed_files.get(key)
            stored_path = Path(stored) if stored else None
            if stored_path is not None and not stored_path.is_absolute():
                stored_path = self.output_dir / stored_path
            event = {
                "key": key,
                "url": url,
                "path": str(stored_path.resolve()) if stored_path else "",
                "exists": bool(stored_path and stored_path.is_file()),
            }
            print(f"[HLS HISTORY] {json.dumps(event, ensure_ascii=False)}", flush=True)
            return
        with self.lock:
            if key in self.hls_queued:
                return
            target, lock_path = self._reserve_hls_target(title or f"Replay {len(self.completed_keys) + 1:02d}")
            self.hls_queued.add(key)
        self.hls_tasks.put(DownloadTask(url, headers or {}, title, target, lock_path))
        print(f"[HLS DOWNLOAD QUEUED] {target}", flush=True)

    def _run_hls(self) -> None:
        while True:
            task = self.hls_tasks.get()
            try:
                self._download_hls(task)
            except Exception as exc:
                print(f"[HLS DOWNLOAD FAILED] {task.url}\n{exc}", flush=True)
            finally:
                self.hls_tasks.task_done()

    def _reserve_hls_target(self, title: str) -> tuple[Path, Path]:
        base_name = timestamped_stem(safe_name(title))
        for index in range(1, 1000):
            suffix = "" if index == 1 else f" ({index})"
            target = self.output_dir / f"{base_name}{suffix}.mp4"
            lock_path = target.with_name(target.name + ".lock")
            if target.exists() and target.stat().st_size > 0:
                if index == 1:
                    continue
                continue
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
                os.close(fd)
                return target, lock_path
            except FileExistsError:
                continue
        raise RuntimeError(f"Could not reserve an output file for {base_name}")

    def _download_hls(self, task: DownloadTask) -> None:
        key = urlsplit(task.url).path
        target = task.target
        lock_path = task.lock_path
        if target is None or lock_path is None:
            target, lock_path = self._reserve_hls_target(task.title or f"Replay {len(self.completed_keys) + 1:02d}")
        partial = target.with_name(target.name + ".part")
        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-loglevel",
            "warning",
            "-http_proxy",
            f"http://{self.upstream_proxy_host}:{self.upstream_proxy_port}",
            "-i",
            task.url,
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-movflags",
            "+frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            str(partial),
        ]
        print(f"[HLS DOWNLOAD START] {target.name}", flush=True)
        try:
            completed = subprocess.run(command, capture_output=True, text=True)
            if completed.returncode != 0:
                partial.unlink(missing_ok=True)
                detail = completed.stderr.strip() or "FFmpeg failed"
                raise RuntimeError(f"{target.name}: {detail}")
            if not partial.exists() or partial.stat().st_size == 0:
                raise RuntimeError("FFmpeg finished without producing output")
            partial.replace(target)
            self.mark_completed(key, str(target.resolve()))
            print(f"[HLS DOWNLOAD COMPLETE] {target}", flush=True)
        finally:
            lock_path.unlink(missing_ok=True)

    def _run(self) -> None:
        while True:
            task = self.tasks.get()
            try:
                self._download(task)
            except Exception as exc:
                print(f"[DOWNLOAD FAILED] {task.url}\n{exc}", flush=True)
            finally:
                self.tasks.task_done()

    def _download(self, task: DownloadTask) -> None:
        target = self.output_dir / safe_filename(task.url, task.title)
        partial = target.with_name(target.name + ".part")
        if target.exists() and target.stat().st_size > 0:
            self.mark_completed(media_key(task.url), str(target.resolve()))
            print(f"[DOWNLOAD SKIPPED] Already exists: {target}", flush=True)
            return

        allowed_headers = {"user-agent", "referer", "origin", "cookie", "authorization", "accept"}
        headers = {key: value for key, value in task.headers.items() if key.lower() in allowed_headers}
        request = Request(task.url, headers=headers)
        print(f"[DOWNLOAD START] {target.name}", flush=True)
        with self.opener.open(request, timeout=60) as response, partial.open("wb") as output:
            total = int(response.headers.get("Content-Length", "0") or 0)
            downloaded = 0
            next_report = 10
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                if total:
                    percent = downloaded * 100 // total
                    if percent >= next_report:
                        print(f"[DOWNLOAD] {target.name}: {percent}%", flush=True)
                        next_report = min(100, (percent // 10 + 1) * 10)
        partial.replace(target)
        self.mark_completed(media_key(task.url), str(target.resolve()))
        print(f"[DOWNLOAD COMPLETE] {target}", flush=True)
