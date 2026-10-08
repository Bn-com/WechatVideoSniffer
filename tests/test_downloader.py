import json
from pathlib import Path

from downloader import find_video_urls, is_downloadable_video_url, safe_filename


def test_find_video_urls_recursively():
    payload = {
        "data": [
            {"playUrl": "https://cdn.test/a.mp4?token=1"},
            {"nested": {"text": r"prefix https:\/\/cdn.test\/b.webm?x=2 suffix"}},
            {"image": "https://cdn.test/cover.jpg"},
        ]
    }
    assert find_video_urls(payload) == {
        "https://cdn.test/a.mp4?token=1",
        "https://cdn.test/b.webm?x=2",
    }


def test_downloadable_url_and_safe_filename():
    assert is_downloadable_video_url("https://x.test/path/movie.MP4?token=x")
    assert not is_downloadable_video_url("https://x.test/path/index.m3u8")
    assert safe_filename("https://x.test/path/%E7%AC%AC%E4%B8%80%E8%AF%BE.mp4?x=1") == "第一课.mp4"


def test_course_title_matches_cdn_quality_suffix(tmp_path):
    from downloader import VideoDownloader, media_key

    downloader = VideoDownloader({"auto_download": False, "output_dir": str(tmp_path)}, tmp_path)
    original = tmp_path / "8c517e40fb44caf1aadb0c48c6ac70c1_2.mp4"
    original.write_bytes(b"video")
    downloader.register_courses({"rows": [{"ext_access_no": "8c517e40fb44caf1aadb0c48c6ac70c1_8", "video_name": "第一节 筹码理论：涅槃重生"}]})

    assert media_key("https://cdn.test/8c517e40fb44caf1aadb0c48c6ac70c1_2.mp4") == media_key("8c517e40fb44caf1aadb0c48c6ac70c1_8")
    assert not original.exists()
    assert (tmp_path / "01. 筹码理论_涅槃重生.mp4").read_bytes() == b"video"


def test_safe_filename_uses_course_title():
    assert safe_filename("https://cdn.test/hash_2.mp4", "第二节 小资金/大人生") == "02. 小资金_大人生.mp4"


def test_builds_polyv_mp4_url_from_access_number():
    from downloader import polyv_mp4_url

    assert polyv_mp4_url("8c517e40fb53c5a7254fe730cdf8684c_8") == (
        "https://mpv.videocc.net/8c517e40fb/c/"
        "8c517e40fb53c5a7254fe730cdf8684c_2.mp4"
    )


def test_formats_all_supported_course_numbers():
    from downloader import format_course_title

    assert format_course_title("第一节 筹码理论") == "01. 筹码理论"
    assert format_course_title("第十节-筹码形态") == "10. 筹码形态"
    assert format_course_title("第十一节-拐点狙击") == "11. 拐点狙击"
    assert format_course_title("第二十一节-交易心态") == "21. 交易心态"