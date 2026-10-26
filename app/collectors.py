"""Proxy e cookies dos coletores: validação do que se cola no painel e versão mascarada.

Módulo puro (sem FastAPI). ValueError traz a mensagem em português que vira HTTP 400.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit

PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
# Cookies que o anyx usa para saber quem está logado (os outros do x.com também passam).
LOGIN_COOKIES = ("auth_token", "ct0", "twid", "kdt")
_TOKEN = re.compile(r"[0-9a-fA-F]{40}")


def _colon_proxy(text: str) -> str:
    """ip:porta:usuário:senha (formato dos vendedores de proxy) vira usuário:senha@ip:porta."""
    scheme, sep, rest = text.rpartition("://")
    if "@" in rest:
        return text
    host, port, *auth = rest.split(":", 3)
    if len(auth) != 2 or not port.isdigit():
        return text
    user, password = (quote(value, safe="") for value in auth)
    return f"{scheme}{sep}{user}:{password}@{host}:{port}"


def parse_proxy(raw: str) -> str:
    """Normaliza esquema://[usuário:senha@]host:porta (ou ip:porta:usuário:senha). Sem esquema, assume http."""
    text = _colon_proxy(raw.strip())
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise ValueError("Proxy inválido: a porta precisa ser um número.") from exc
    scheme = parts.scheme.lower()
    if scheme not in PROXY_SCHEMES:
        raise ValueError("Proxy inválido: use http://, https:// ou socks5://.")
    if not parts.hostname or port is None:
        raise ValueError("Proxy inválido: informe host e porta (ex: http://usuario:senha@1.2.3.4:8000).")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("Proxy inválido: só esquema, usuário, senha, host e porta.")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    auth = ""
    if parts.username is not None:
        auth = quote(unquote(parts.username), safe="")
        if parts.password is not None:
            auth += ":" + quote(unquote(parts.password), safe="")
        auth += "@"
    return f"{scheme}://{auth}{host}:{port}"


def mask_proxy(proxy: str | None) -> str | None:
    if not proxy:
        return None
    parts = urlsplit(proxy)
    host = f"[{parts.hostname}]" if parts.hostname and ":" in parts.hostname else parts.hostname
    auth = ""
    if parts.username is not None:
        auth = unquote(parts.username) + (":***" if parts.password is not None else "") + "@"
    return f"{parts.scheme}://{auth}{host}:{parts.port}"


def parse_cookies(raw: str) -> dict[str, str]:
    """Aceita o auth_token sozinho, o cabeçalho Cookie (a=1; b=2) ou o JSON exportado por extensão.

    Devolve {nome: valor} só com cookies do x.com / twitter.com. Exige auth_token.
    """
    text = raw.strip()
    if not text:
        raise ValueError("Cole os cookies ou o auth_token.")
    cookies: dict[str, str] = {}
    if _TOKEN.fullmatch(text):
        cookies["auth_token"] = text
    elif text[0] in "[{":
        try:
            data: Any = json.loads(text)
        except ValueError as exc:
            raise ValueError("Cookies inválidos: o JSON não abre.") from exc
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            data = data["cookies"]
        if isinstance(data, dict):
            cookies = {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int))}
        elif isinstance(data, list):
            for item in data:
                if not isinstance(item, dict) or "name" not in item or "value" not in item:
                    continue
                domain = str(item.get("domain") or ".x.com")
                if domain.endswith("x.com") or domain.endswith("twitter.com"):
                    cookies[str(item["name"])] = str(item["value"])
        else:
            raise ValueError("Cookies inválidos: esperava uma lista de cookies.")
    else:
        for piece in text.removeprefix("Cookie:").split(";"):
            name, sep, value = piece.strip().partition("=")
            if sep and name.strip():
                cookies[name.strip()] = value.strip()
    token = cookies.get("auth_token", "")
    if not _TOKEN.fullmatch(token):
        raise ValueError("Cookies sem auth_token válido (40 caracteres hexadecimais).")
    return cookies


def dump_cookies(cookies: dict[str, str]) -> str:
    return json.dumps(cookies, separators=(",", ":"), sort_keys=True)


def mask_cookies(stored: str | None) -> str | None:
    """auth_token …a1b2 + ct0, twid (3 cookies). Nunca devolve um valor inteiro."""
    if not stored:
        return None
    try:
        cookies = json.loads(stored)
    except ValueError:
        return "cookies ilegíveis"
    token = str(cookies.get("auth_token", ""))
    others = [name for name in LOGIN_COOKIES[1:] if name in cookies]
    text = f"auth_token …{token[-4:]}" if token else "sem auth_token"
    if others:
        text += " + " + ", ".join(others)
    return f"{text} ({len(cookies)} cookies)"
