"""Fontes do Radar de trend: leitura pura do que cada site devolve (sem rede aqui; app/radar.py busca).

Cada parser devolve listas simples e nunca levanta exceção: página que mudou de formato vira lista vazia, e o
radar mostra a fonte como indisponível em vez de quebrar.
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

URLS = {
    "x": "https://trends24.in/brazil/",
    "x_world": "https://trends24.in/",
    "google": "https://trends.google.com/trending/rss?geo=BR",
    "google_world": "https://trends.google.com/trending/rss?geo=US",
    "news": "https://news.google.com/rss?hl=pt-BR&gl=BR&ceid=BR:pt-419",
}
HT = "{https://trends.google.com/trending/rss}"


def norm(text: str) -> str:
    """Forma de comparar assuntos: minúsculas, sem acento, sem # e @, só letras, números e espaços únicos."""
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", plain).split())


def latin(text: str) -> bool:
    """Só letras do alfabeto latino (com acentos)? Emoji, número e pontuação não contam. Tira do Brasil os
    trends em tailandês, árabe, coreano, japonês etc. que o X Brasil às vezes mostra."""
    for char in text or "":
        if char.isalpha() and ord(char) > 0x024F and not 0x1E00 <= ord(char) <= 0x1EFF:
            return False
    return True


class _XParser(HTMLParser):
    """Uma lista por hora: o h3 com data-timestamp abre a hora, cada a.trend-link é um assunto em ordem."""

    def __init__(self) -> None:
        super().__init__()
        self.hours: list[dict[str, Any]] = []
        self.in_link = False
        self.text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "h3" and values.get("data-timestamp"):
            try:
                at = datetime.fromtimestamp(float(values["data-timestamp"]), tz=timezone.utc)
            except ValueError:
                return
            self.hours.append({"at": at, "items": []})
        elif tag == "a" and "trend-link" in (values.get("class") or "").split() and self.hours:
            self.in_link, self.text = True, ""

    def handle_data(self, data: str) -> None:
        if self.in_link:
            self.text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.in_link:
            self.in_link = False
            title = self.text.strip()
            if title:
                self.hours[-1]["items"].append(title)


def parse_x(html: str) -> list[dict[str, Any]]:
    """Trending topics do X Brasil por hora (trends24), a hora mais nova primeiro."""
    parser = _XParser()
    try:
        parser.feed(html or "")
    except Exception:
        return []
    hours = [hour for hour in parser.hours if hour["items"]]
    return sorted(hours, key=lambda hour: hour["at"], reverse=True)


def _items(xml: str) -> list[ET.Element]:
    try:
        return ET.fromstring(xml).findall("./channel/item")
    except (ET.ParseError, ValueError):
        return []


def _text(node: ET.Element, path: str) -> str:
    found = node.find(path)
    return (found.text or "").strip() if found is not None else ""


def parse_google(xml: str) -> list[dict[str, Any]]:
    """Buscas em alta no Brasil, em ordem, com o volume aproximado e as notícias ligadas."""
    out = []
    for item in _items(xml):
        title = _text(item, "title")
        if not title:
            continue
        news = [
            {"title": _text(n, f"{HT}news_item_title"), "url": _text(n, f"{HT}news_item_url"),
             "source": _text(n, f"{HT}news_item_source")}
            for n in item.findall(f"{HT}news_item")
        ]
        out.append({"title": title, "volume": _text(item, f"{HT}approx_traffic") or None,
                    "news": [n for n in news if n["title"] and n["url"]]})
    return out


def parse_news(xml: str) -> list[dict[str, Any]]:
    """Manchetes do Google Notícias Brasil; o " - Fonte" do fim do título vira o campo source."""
    out = []
    for item in _items(xml):
        title, url = _text(item, "title"), _text(item, "link")
        if not title or not url:
            continue
        source = _text(item, "source")
        head, sep, tail = title.rpartition(" - ")
        if sep and head:
            title, source = head.strip(), source or tail.strip()
        out.append({"title": title, "url": url, "source": source})
    return out
