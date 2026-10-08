from sniffer import classify_video, normalized_content_type, url_extension


def test_extension_ignores_query_string():
    assert url_extension("https://cdn.example/video.MP4?token=x") == ".mp4"


def test_detects_video_extensions():
    assert classify_video("https://example.test/a.mp4", "") == ("MP4", False)
    assert classify_video("https://example.test/a.m3u8", "") == ("M3U8", False)
    assert classify_video("https://example.test/a.ts", "") == ("TS", True)
    assert classify_video("https://example.test/a.m4s", "") == ("M4S", True)


def test_detects_content_type_without_extension():
    assert classify_video("https://example.test/api?id=1", "video/mp4") == ("MP4", False)
    assert classify_video("https://example.test/api", "application/vnd.apple.mpegurl") == ("M3U8", False)


def test_content_type_is_case_insensitive_and_strips_parameters():
    assert normalized_content_type("Video/WebM; charset=binary") == "video/webm"
    assert classify_video("https://example.test/api", "Video/WebM; charset=binary") == ("WEBM", False)


def test_non_video_is_ignored():
    assert classify_video("https://example.test/api", "application/json") is None

def test_response_headers_streams_video(tmp_path, monkeypatch):
    from mitmproxy.test import tflow
    from sniffer import VideoSniffer

    monkeypatch.setenv("WVS_CONFIG", str(tmp_path / "missing.json"))
    addon = VideoSniffer()
    flow = tflow.tflow(resp=True)
    flow.request.path = "/movie.mp4"
    flow.response.headers["content-type"] = "video/mp4"

    addon.responseheaders(flow)

    assert flow.response.stream is True
    assert flow.request.pretty_url in addon.seen_urls