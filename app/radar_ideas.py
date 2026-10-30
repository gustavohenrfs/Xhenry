"""Ideias de post para contas de texto (modelos +18 no X) a partir de um assunto do Radar.

O tom é o dos exemplos do dono do painel: promessa ("se o Flamengo ganhar eu…"), pergunta provocante à torcida ou
aos fãs, "quem tá vendo comigo?", com "rs" e emoji. Regras fixas, nos modelos e no pedido ao Grok:
- sugestivo, nunca explícito;
- nunca sexualizar uma pessoa real com nome (a provocação vai para a torcida, o evento ou a situação);
- nada com menor de idade; assunto bloqueado pelo radar (morte, tragédia, crime, doença, criança) não ganha ideia.

ideas() é grátis (modelos por tipo de assunto); ask_grok_lines() usa a chave xAI do usuário.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

from app import radar_sources as src

MAX_IDEAS = 5
# Torcida de cada time (norm do time → (nome para a frase, torcida)).
TEAMS = {
    "flamengo": ("Flamengo", "flamenguistas"), "mengao": ("Mengão", "flamenguistas"),
    "corinthians": ("Corinthians", "corintianos"), "timao": ("Timão", "corintianos"),
    "palmeiras": ("Palmeiras", "palmeirenses"), "verdao": ("Verdão", "palmeirenses"),
    "vasco": ("Vasco", "vascaínos"), "sao paulo": ("São Paulo", "são-paulinos"), "spfc": ("São Paulo", "são-paulinos"),
    "gremio": ("Grêmio", "gremistas"), "internacional": ("Inter", "colorados"), "inter": ("Inter", "colorados"),
    "cruzeiro": ("Cruzeiro", "cruzeirenses"), "atletico": ("Galo", "atleticanos"), "galo": ("Galo", "atleticanos"),
    "botafogo": ("Botafogo", "botafoguenses"), "fluminense": ("Fluminense", "tricolores"), "santos": ("Santos", "santistas"),
    "bahia": ("Bahia", "tricolores baianos"), "fortaleza": ("Fortaleza", "torcedores do Leão"),
    "sport": ("Sport", "rubro-negros"), "vitoria": ("Vitória", "rubro-negros baianos"), "selecao": ("Seleção", "brasileiros"),
}
FOOTBALL_TEAM = [
    "Se o {team} ganhar hoje eu vou fazer uma coisa muuuito pra frente rs",
    "Fiquei sabendo que os {fans} são os mais safadinhos… é verdade? 👀",
    "Quem vai ver o jogo do {team} comigo hoje? Prometo torcer de um jeito diferente rs",
    "Se o {team} fizer gol eu mando fotinha pra quem comentar 🔥",
    "Não sei por quê, mas os {fans} sempre foram meus preferidos rs",
]
FOOTBALL = [
    "Se meu time ganhar hoje eu posto aquela foto que vocês pediram rs",
    "{topic} hoje e eu só penso no pós-jogo 😏",
    "Quem tá vendo {topic} comigo? Comenta o placar que eu respondo do meu jeito 👀",
    "Torcedor nervoso é o mais carinhoso, concordam? rs",
]
POLITICS = [
    "Se {topic} ganhar eu mando fotinha 👀",
    "Todo mundo brigando por causa de {topic} e eu aqui só querendo atenção rs",
    "Política me deixa nervosa… alguém vem me acalmar? rs",
    "Quem acertar o que vai acontecer com {topic} ganha um presentinho meu 😏",
]
TV = [
    "Quem tá assistindo {topic}? Comenta que eu respondo todo mundo 👀",
    "Se eu entrasse no {topic} vocês votavam em mim? rs",
    "Assistindo {topic} de pijaminha… ou sem rs",
    "Time de quem em {topic}? Me conta que eu conto o meu 😏",
]
MUSIC = [
    "Quem vai no show de {topic}? Me leva rs",
    "Ouvindo {topic} e pensando besteira rs",
    "Fã de {topic} por aqui? Me conta nos comentários 👀",
    "Me manda a música de {topic} que te lembra de mim 😏",
]
DATES = [
    "Feliz {topic}! Qual presente vocês querem de mim? rs",
    "{topic} chegando e eu já sei como vou comemorar 😏",
    "Quem vai passar o {topic} comigo? Comenta que eu respondo 👀",
    "No {topic} eu vou ser boazinha… ou não rs",
]
# Pessoa real (atriz, cantor, jogador…): a frase fala do burburinho, nunca da pessoa em si.
PERSON = [
    "Todo mundo falando de {topic} hoje… alguém me conta o que rolou? 👀",
    "{topic} nos trends e eu aqui só querendo a atenção de vocês rs",
    "Vi {topic} bombando e fiquei curiosa, quem me explica? Respondo todo mundo 😏",
    "Enquanto falam de {topic}, eu tô aqui esperando vocês comentarem rs",
]
# Programa, novela, reality: o que faz o assunto ser "assistível" (senão, com cara de nome, é pessoa).
SHOW_RE = re.compile(r"\b(?:bbb|big brother|novela|reality|serie|filme|a fazenda|globo|sbt|record|netflix|capitulo|"
                     r"programa|final|episodio|temporada|ilha|masterchef|the voice|show|copa|jogo)\b")
ACRONYMS = {"bbb", "stf", "pec", "cpi", "ufc", "nba", "nfl", "psg", "inss", "pix", "enem", "cbf", "fifa", "pl", "pt", "psol", "mdb"}
SMALL_WORDS = {"de", "do", "da", "dos", "das", "e", "x", "vs", "em", "no", "na", "nos", "nas", "o", "a", "os", "as"}
GENERIC = [
    "Todo mundo falando de {topic} e eu aqui pensando em outra coisa rs",
    "Alguém me explica {topic}? Em troca eu… rs",
    "{topic} nos trends e eu sem entender nada, me ensina? 👀",
    "Comenta {topic} que eu respondo do meu jeito 😏",
]


def clean_title(title: str) -> str:
    """"#Flamengo" → "Flamengo"; hashtag CamelCase vira palavras; tudo minúsculo ou tudo maiúsculo vira
    "Copa do Brasil Sub 20" (palavras pequenas ficam minúsculas)."""
    text = (title or "").strip()
    if text.startswith("#"):
        text = re.sub(r"(?<=[a-zà-ú])(?=[A-ZÀ-Ú])", " ", text[1:])
    words = text.split()
    if words and (text == text.lower() or text == text.upper()):
        def tidy(i: int, word: str) -> str:
            low = word.lower()
            if i and low in SMALL_WORDS:
                return low
            if src.norm(word) in ACRONYMS:
                return word.upper()  # BBB, STF, PEC
            return word[:1].upper() + word[1:].lower()

        words = [tidy(i, w) for i, w in enumerate(words)]
    return " ".join(words)[:60]


def is_person(norm: str, category: str) -> bool:
    """Famosos/TV sem palavra de programa, com 2 a 4 palavras: é nome de gente."""
    return category == "Famosos/TV" and not SHOW_RE.search(norm) and 2 <= len(norm.split()) <= 4


def team_of(norm: str) -> tuple[str, str] | None:
    for key, value in TEAMS.items():
        if re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", norm):
            return value
    return None


def ideas(topic: dict[str, Any], kind: str | None = None) -> list[str]:
    """3 a 5 frases prontas para o assunto. kind="data" para datas sazonais. Bloqueado = lista vazia."""
    if topic.get("blocked"):
        return []
    title = clean_title(topic.get("title") or topic.get("name") or "")
    if not title:
        return []
    norm = topic.get("norm") or src.norm(title)
    category = topic.get("category") or ""
    team = team_of(norm)
    if kind == "data":
        lines = DATES
    elif team:
        lines = FOOTBALL_TEAM
    elif topic.get("sources") == ["wiki"] and 2 <= len(norm.split()) <= 4:
        lines = PERSON  # verbete da Wikipédia com nome de gente
    elif category == "Futebol":
        lines = FOOTBALL
    elif category == "Política":
        # "Se X ganhar" só faz sentido com nome curto (Lula, Tarcísio), não com frase de protesto.
        lines = POLITICS if len(title.split()) <= 2 else POLITICS[1:]
    elif is_person(norm, category):
        lines = PERSON
    elif category == "Famosos/TV":
        lines = TV
    elif category == "Música":
        lines = MUSIC
    else:
        lines = GENERIC
    out: list[str] = []
    for line in lines:
        text = line.format(topic=title, team=team[0] if team else title, fans=team[1] if team else "")
        text = text[0].upper() + text[1:]
        if text not in out:
            out.append(text)
    return out[:MAX_IDEAS]


def grok_prompt(topic: dict[str, Any]) -> str:
    title = clean_title(topic.get("title") or topic.get("name") or "")
    return (
        "Você escreve posts curtos de texto para contas de modelos adultas (+18) no X, em português do Brasil, para "
        "pegar carona num assunto em alta e gerar comentários.\n"
        f"Assunto em alta agora: {title} (categoria: {topic.get('category') or 'geral'}).\n"
        "Tom dos posts que funcionam: \"Se o Lula ganhar nessa eleição eu mando fotinha\", \"Se o Flamengo ganhar eu "
        "vou fazer uma coisa muuito pra frente rs\", \"Fiquei sabendo que os flamenguistas são os mais safadinhos… é "
        "verdade? 👀\". Promessa, pergunta provocante à torcida ou aos fãs, \"quem tá vendo comigo?\", com rs e emoji.\n"
        "Regras: sugestivo, nunca explícito (sem descrever sexo nem partes do corpo); nunca sexualizar uma pessoa "
        "real com nome (a provocação vai para a torcida, o evento ou a situação); nada envolvendo menor de idade; "
        "nada de ódio ou ofensa.\n"
        "Escreva 5 posts diferentes, um por linha, sem numeração, cada um com até 200 caracteres."
    )


def parse_lines(text: str) -> list[str]:
    out = []
    for raw in (text or "").splitlines():
        line = re.sub(r"^\s*(?:\d+\s*[.)-]|[-•*])\s*", "", raw).strip().strip('"').strip()
        if line and line not in out:
            out.append(line[:280])
    return out[:MAX_IDEAS]


def ask_grok_lines(key: str, prompt: str) -> str:
    """Texto do Grok para o pedido (chat completions da xAI). Erro vira ValueError com mensagem legível."""
    from app.services import grok_text

    model = os.getenv("XAI_MODEL", "grok-4.6")
    try:
        with httpx.Client(timeout=60.0, follow_redirects=False) as client:
            response = client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}]},
            )
    except httpx.HTTPError as exc:
        raise ValueError("Sem resposta da xAI. Tente de novo.") from exc
    if response.status_code >= 400:
        raise ValueError("A xAI recusou o pedido. Confira a chave e o modelo em Configurações.")
    try:
        return grok_text(response.json())
    except ValueError as exc:
        raise ValueError("A xAI não devolveu texto legível.") from exc
