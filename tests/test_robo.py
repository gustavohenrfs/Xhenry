"""robo.py sem banco: repassador de proxy (HTTP CONNECT e SOCKS5 com senha) e regras puras.

Os proxies e o destino são servidores falsos em 127.0.0.1; nada sai para a internet.
"""

from __future__ import annotations

import base64
import socket
import threading
from datetime import datetime, timedelta, timezone

import pytest

import robo


def _pipe_both(a, b):
    threading.Thread(target=robo._pipe, args=(a, b), daemon=True).start()
    robo._pipe(b, a)


def _serve(handler):
    server = socket.create_server(("127.0.0.1", 0))

    def loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return server, server.getsockname()[1]


class FakeNet:
    """Eco (devolve em maiúsculas), proxy HTTP com user:p@ss:wd e SOCKS5 com sock:senha."""

    def __init__(self):
        self.targets: list[tuple[str, int]] = []
        self.servers = []
        for handler in (self.echo, self.http_proxy, self.socks_proxy):
            server, port = _serve(handler)
            self.servers.append(server)
            setattr(self, handler.__name__ + "_port", port)

    def close(self):
        for server in self.servers:
            server.close()

    @staticmethod
    def echo(conn):
        while True:
            data = conn.recv(4096)
            if not data:
                break
            conn.sendall(data.upper())
        conn.close()

    def http_proxy(self, conn):
        head = robo._read_head(conn).decode("latin-1")
        want = "Proxy-Authorization: Basic " + base64.b64encode(b"user:p@ss:wd").decode()
        if want not in head:
            conn.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            conn.close()
            return
        host, _, port = head.split("\r\n")[0].split(" ")[1].rpartition(":")
        self.targets.append((host, int(port)))
        upstream = socket.create_connection(("127.0.0.1", self.echo_port), timeout=10)
        conn.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
        _pipe_both(conn, upstream)

    def socks_proxy(self, conn):
        _ver, n = robo._recv_exact(conn, 2)
        if 2 not in robo._recv_exact(conn, n):
            conn.sendall(b"\x05\xff")
            conn.close()
            return
        conn.sendall(b"\x05\x02")
        robo._recv_exact(conn, 1)
        user = robo._recv_exact(conn, robo._recv_exact(conn, 1)[0])
        password = robo._recv_exact(conn, robo._recv_exact(conn, 1)[0])
        if (user, password) != (b"sock", b"senha"):
            conn.sendall(b"\x01\x01")
            conn.close()
            return
        conn.sendall(b"\x01\x00")
        req = robo._recv_exact(conn, 5)
        host = robo._recv_exact(conn, req[4]).decode()
        port = int.from_bytes(robo._recv_exact(conn, 2), "big")
        self.targets.append((host, port))
        upstream = socket.create_connection(("127.0.0.1", self.echo_port))
        conn.sendall(b"\x05\x00\x00\x01" + bytes(4) + b"\x00\x00")
        _pipe_both(conn, upstream)


@pytest.fixture
def net():
    fake = FakeNet()
    yield fake
    fake.close()


def through(relay, target=b"eco.test:443"):
    """Faz o papel do Chrome: CONNECT no repassador, sem senha."""
    c = socket.create_connection(("127.0.0.1", int(relay.url.rsplit(":", 1)[1])), timeout=10)
    c.sendall(b"CONNECT " + target + b" HTTP/1.1\r\nHost: " + target + b"\r\n\r\n")
    status = robo._read_head(c).split(b"\r\n")[0]
    if not status.endswith(b"200 Connection established"):
        c.close()
        return status, None
    c.sendall(b"ola coletor")
    reply = c.recv(100)
    c.close()
    return status, reply


def test_http_proxy_with_password(net):
    # Senha com @ e :, codificada como o painel salva.
    relay = robo.ProxyRelay(f"http://user:p%40ss%3Awd@127.0.0.1:{net.http_proxy_port}")
    try:
        _status, reply = through(relay)
        assert reply == b"OLA COLETOR"
        assert net.targets[-1] == ("eco.test", 443)
    finally:
        relay.close()


def test_http_wrong_password(net):
    relay = robo.ProxyRelay(f"http://user:errada@127.0.0.1:{net.http_proxy_port}")
    try:
        status, reply = through(relay)
        assert b"502" in status and reply is None
        assert relay.failures and "407" in relay.failures[-1]
        with pytest.raises(robo.ProxyError, match="407"):
            relay.probe()
    finally:
        relay.close()


def test_socks5_with_password(net):
    relay = robo.ProxyRelay(f"socks5://sock:senha@127.0.0.1:{net.socks_proxy_port}")
    try:
        _status, reply = through(relay)
        assert reply == b"OLA COLETOR" and net.targets[-1] == ("eco.test", 443)
        relay.probe()
        assert net.targets[-1] == ("x.com", 443)
    finally:
        relay.close()
    bad = robo.ProxyRelay(f"socks5://sock:errada@127.0.0.1:{net.socks_proxy_port}")
    try:
        with pytest.raises(robo.ProxyError, match="usuário/senha"):
            bad.probe()
    finally:
        bad.close()


def test_proxy_down():
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    relay = robo.ProxyRelay(f"http://u:p@127.0.0.1:{port}", timeout=3)
    try:
        with pytest.raises(robo.ProxyError, match="não conectou"):
            relay.probe()
    finally:
        relay.close()


def test_plain_http_is_refused(net):
    relay = robo.ProxyRelay(f"http://user:p%40ss%3Awd@127.0.0.1:{net.http_proxy_port}")
    try:
        c = socket.create_connection(("127.0.0.1", int(relay.url.rsplit(":", 1)[1])), timeout=10)
        c.sendall(b"GET http://exemplo.com/ HTTP/1.1\r\nHost: exemplo.com\r\n\r\n")
        assert b"405" in c.recv(200)
        c.close()
    finally:
        relay.close()


def _exc(name, message=""):
    return type(name, (Exception,), {})(message)


@pytest.mark.parametrize(("exc", "kind"), [
    (_exc("RateLimited", "429"), "limite"),
    (_exc("SessionExpired", "login wall"), "sessao"),
    (_exc("NoLogin"), "sessao"),
    (_exc("ProxyError", "recusada"), "proxy"),
    (_exc("Error", "net::ERR_TUNNEL_CONNECTION_FAILED"), "proxy"),
    (_exc("NotFound", "nothing loaded at https://x.com/a (no UserTweets)"), "bloqueio"),
    (_exc("UIChanged", "unexpected page"), "bloqueio"),
    (_exc("NotFound", "@a is unavailable (Suspended)"), "conta"),
    (_exc("KeyError", "x"), "erro"),
])
def test_classify(exc, kind):
    assert robo.classify(exc) == kind


def test_pause_until_uses_x_reset_on_429():
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    exc = _exc("RateLimited")
    exc.reset = int((now + timedelta(minutes=20)).timestamp())
    assert robo.pause_until("limite", exc, now) == now + timedelta(minutes=20, seconds=60)
    assert robo.pause_until("bloqueio", _exc("NotFound"), now) == now + timedelta(minutes=30)


def test_pairs_for():
    collectors = [{"id": 1}, {"id": 2}]
    accounts = [{"id": 10}, {"id": 11}, {"id": 12}]
    auto = robo.pairs_for(collectors, accounts[:2], manual=False)
    assert [(c["id"], a["id"]) for c, a in auto] == [(1, 10), (2, 11)]
    manual = robo.pairs_for(collectors, accounts, manual=True)
    assert [(c["id"], a["id"]) for c, a in manual] == [(1, 10), (2, 11), (1, 12)]


def test_views_drop():
    assert robo.views_drop(60, 100) == 40
    assert robo.views_drop(80, 100) is None   # caiu menos que QUEDA_LIMITE
    assert robo.views_drop(0, 50) is None     # ontem pequeno demais


def test_tweet_metrics_reads_bookmarks():
    tweet = {"legacy": {"favorite_count": 5, "bookmark_count": 7, "quote_count": 2}, "views": {"count": "900"}}
    metrics = robo.tweet_metrics(tweet)
    assert (metrics["bookmark_count"], metrics["quote_count"], metrics["view_count"]) == (7, 2, 900)
    assert robo.tweet_metrics({"id": "1"})["bookmark_count"] == 0


@pytest.mark.parametrize(
    ("views", "suffix"),
    [(500, None), (501, ""), (10_000, ""), (10_001, "bombando"), (150_000, "100k"), (1_000_001, "1m")],
)
def test_view_level_picks_the_highest_reached(views, suffix):
    level = robo.view_level(views)
    assert (level[0] if level else None) == suffix


def test_view_alert_key_keeps_the_old_key_for_the_first_level():
    assert robo.view_alert_key(7, "123", "") == "viral:7:123"
    assert robo.view_alert_key(7, "123", "1m") == "viral:7:123:1m"
    assert robo.fmt_int(1234567) == "1.234.567"
