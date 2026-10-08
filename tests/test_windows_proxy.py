from dataclasses import asdict

from windows_proxy import ProxySession, ProxyState


def test_proxy_state_round_trip():
    original = ProxyState(1, "127.0.0.1:10808", "<local>", None, 0)
    assert ProxyState.from_dict(asdict(original)) == original


def test_proxy_session_persists_and_restores(tmp_path, monkeypatch):
    written = []
    original = ProxyState(1, "127.0.0.1:10808", "<local>", None, 0)
    monkeypatch.setattr("windows_proxy.read_proxy_state", lambda: original)
    monkeypatch.setattr("windows_proxy.write_proxy_state", written.append)
    session = ProxySession(tmp_path / "proxy_backup.json")

    active = session.activate("127.0.0.1", 8888)
    assert active.proxy_server == "127.0.0.1:8888"
    assert session.has_backup

    assert session.restore()
    assert written == [active, original]
    assert not session.has_backup


def test_stale_backup_recovery(tmp_path, monkeypatch):
    written = []
    backup = tmp_path / "proxy_backup.json"
    backup.write_text('{"proxy_enable": 1, "proxy_server": "127.0.0.1:10808"}', encoding="utf-8")
    monkeypatch.setattr("windows_proxy.write_proxy_state", written.append)

    session = ProxySession(backup)
    assert session.recover_if_needed()
    assert written[0].proxy_server == "127.0.0.1:10808"
    assert not backup.exists()
