"""Gerador por palavras-chave (aba Radar): você digita as palavras ("Pitbull amigo do Fejuca, Elisa Sanches, live,
vazou") e ele monta frases de todos os formatos que viralizam no X (manchete, pergunta, polêmica, enquete,
desafio, promessa, confissão, urgência, duplo sentido), grátis e sem API, com uma nota de potencial viral.

Como lê as palavras (cada vírgula separa um pedaço; o pedaço fica junto):
- gancho: vazou, urgente, eita, bomba… vira a abertura em caixa alta ("VAZOU!!");
- fato: live, foto, vídeo, treta, namoro… vira o acontecimento ("faz live com", "a live do…");
- nome: pedaço com letra maiúscula (pessoa, time, página) — o sujeito da notícia;
- tema: o resto (academia, legging, cama…).

Regras fixas (as mesmas do Radar): nunca link/bio/grupo/Telegram; com nome de pessoa real, nada sexual sobre ela
(o tom quente só entra em promessa de quem posta, com o acontecimento como condição) e palavra sexual junto de
nome é recusada; palavra de tragédia/crime/menor bloqueia; frase que cita outro dia da semana fica de fora.

O banco de cada conjunto de palavras é determinístico (mesmas palavras + mistura = mesmas frases) e passa de mil
frases; a tela pede de 12 em 12 e, quando acaba, troca a mistura.
"""

from __future__ import annotations

import random
import re
from datetime import datetime
from functools import lru_cache
from typing import Any

from app import radar_sources as src
from app.radar_ideas import EMOJI, REWARDS, _cap, _compose, _low, fits_day, now_sp, rules

PAGE = 12
POOL = 1500        # frases por conjunto de palavras e mistura
MAX_TERMS = 8
MAX_LEN = 260

# norm do gancho → como abre a manchete.
HOOKS = {
    "vazou": "VAZOU", "vazaram": "VAZOU", "urgente": "URGENTE", "eita": "EITA", "bomba": "BOMBA", "exclusivo": "EXCLUSIVO",
    "acabou de sair": "ACABOU DE SAIR", "agora": "AGORA", "chocada": "TÔ CHOCADA", "chocado": "CHOCADO", "polemica": "POLÊMICA",
    "socorro": "SOCORRO", "mds": "MDS", "gente": "GENTE", "ultima hora": "ÚLTIMA HORA", "confirmado": "CONFIRMADO",
    "plot twist": "PLOT TWIST", "babado": "BABADO", "fofoca": "FOFOCA", "flagra": "FLAGRA", "exposed": "EXPOSED",
}
DEFAULT_HOOKS = ("URGENTE", "EITA", "ACABOU DE SAIR", "GENTE", "SOCORRO", "BABADO")
# norm do fato → (verbo da manchete, o fato com artigo).
FACTS = {
    "live": ("faz live", "a live"), "lives": ("faz live", "a live"), "foto": ("posta foto", "a foto"),
    "fotos": ("posta fotos", "as fotos"), "video": ("posta vídeo", "o vídeo"), "videos": ("posta vídeos", "os vídeos"),
    "treta": ("arma treta", "a treta"), "briga": ("briga", "a briga"), "beijo": ("aparece aos beijos", "o beijo"),
    "namoro": ("assume namoro", "o namoro"), "termino": ("anuncia término", "o término"), "festa": ("aparece em festa", "a festa"),
    "podcast": ("vai em podcast", "o podcast"), "collab": ("faz collab", "a collab"), "parceria": ("fecha parceria", "a parceria"),
    "entrevista": ("dá entrevista", "a entrevista"), "show": ("faz show", "o show"), "encontro": ("se encontra", "o encontro"),
    "print": ("expõe print da conversa", "o print da conversa"), "prints": ("expõe prints", "os prints"),
    "audio": ("solta áudio", "o áudio"), "story": ("posta story", "o story"), "stories": ("posta stories", "os stories"),
    "indireta": ("solta indireta", "a indireta"), "desabafo": ("faz desabafo", "o desabafo"), "react": ("faz react", "o react"),
    "jogo": ("entra em campo", "o jogo"), "luta": ("sobe no ringue", "a luta"), "desafio": ("aceita desafio", "o desafio"),
    "viagem": ("viaja", "a viagem"), "flagra": ("é flagrado", "o flagra"), "casamento": ("anuncia casamento", "o casamento"),
    "separacao": ("anuncia separação", "a separação"), "polemica": ("se envolve em polêmica", "a polêmica"),
    "aposta": ("faz aposta", "a aposta"), "ensaio": ("faz ensaio", "o ensaio"), "clipe": ("lança clipe", "o clipe"),
    "musica": ("lança música", "a música"), "post": ("faz post", "o post"), "tweet": ("solta tweet", "o tweet"),
}
# Primeiro nome que termina em "a" mas é de homem (o resto em "a" é tratado como feminino).
MALE_A = {"luca", "joshua", "nikola", "andrea", "jonatha", "elia", "costa", "silva", "souza", "ronalda"}
# Vocabulário sexual: junto de nome de pessoa real é recusado (e nunca sai em frase com nome fora da promessa).
SEX_RE = re.compile(
    r"\b(?:nude|nudes|pelad\w*|nua|nu|sexo|transa\w*|goz\w*|boquete|chupa\w*|sentad\w*|sentar|porn\w*|calcinha|"
    r"seios?|peitos?|bunda|buceta|pau|pinto|rola|safad\w*|tesao|molhad\w*|gem\w*|lingerie|quatro|cavalga\w*|"
    r"xvideos|onlyfans|intim\w*|sensual|putaria|punheta|siririca|excitad\w*)\b")

# Formatos do gerador (chave → nome na tela). Os do Radar hot (format_of) continuam valendo para a estimativa.
FORMATS = {
    "manchete": "Manchete", "pergunta": "Pergunta", "polemica": "Polêmica", "enquete": "Enquete", "desafio": "Desafio",
    "promessa": "Promessa", "confissao": "Confissão", "urgencia": "Urgência", "duplo": "Duplo sentido",
}
# Quanto cada formato puxa de resposta no X (o algoritmo entrega o que gera conversa).
FMT_BASE = {"pergunta": 30, "enquete": 32, "polemica": 31, "desafio": 29, "promessa": 30, "manchete": 30,
            "confissao": 25, "urgencia": 22, "duplo": 22}
CLOSERS = ("Sejam sinceros", "Sem mentir", "rs", "👀", "😏", "Responde que eu leio tudo", "Quero ver quem tem coragem de responder",
           "Fala nos comentários", "🤔", "Pode falar, eu aguento rs")
GOALS = ((100, "comentários"), (200, "comentários"), (300, "respostas"), (500, "curtidas"), (1000, "curtidas"), (200, "RT"))
LEVE_REWARDS = ("mando fotinha", "conto o que achei de verdade", "posto uma foto nova", "faço uma coisa muuito pra frente rs",
                "respondo todo mundo", "conto um segredo meu")
POLAR_RE = re.compile(r"\b(?:opiniao impopular|ninguem tem coragem|cancelar|defende|de que lado|meio termo|dividiu|ama ou|"
                      r"odeia|superestimad\w*|hype ou flop|armado|aprovado ou|time )\b")
HOOK_START = re.compile(r"^[A-ZÀ-Ú ]{3,}!!")
ADDRESS_RE = re.compile(r"\b(?:voces|voce|homens|alguem|quem)\b")


class KeywordError(ValueError):
    """Mensagem para a tela (vira 400)."""


# ---- Leitura das palavras ----

def split_terms(raw: str | list[str]) -> list[str]:
    parts = raw if isinstance(raw, list) else re.split(r"[,;\n]+", raw or "")
    out: list[str] = []
    for part in parts:
        clean = " ".join(str(part).split()).strip(" .!?#")
        key = src.norm(ROLE_SUFFIX.sub("", clean))
        if clean and key and all(key != src.norm(ROLE_SUFFIX.sub("", o)) for o in out):
            out.append(clean[:70])
    return out[:MAX_TERMS]


ROLE_SUFFIX = re.compile(r"\s*=\s*(nome|tema)$", re.IGNORECASE)  # "Academia=tema": você corrige o papel na tela


def role_of(term: str) -> str:
    forced = ROLE_SUFFIX.search(term)
    if forced:
        return forced.group(1).lower()
    key = src.norm(term)
    if key in HOOKS:
        return "gancho"
    if key in FACTS:
        return "fato"
    if any(word[:1].isupper() for word in term.split()):
        return "nome"
    return "tema"


def feminine(name: str) -> bool:
    first = src.norm(name).split()[0] if src.norm(name) else ""
    return first.endswith("a") and first not in MALE_A


def de(name: str) -> str:
    return ("da " if feminine(name) else "do ") + name


def art(name: str) -> str:
    return ("a " if feminine(name) else "o ") + name


def d(noun: str) -> str:
    """'a live do X' → 'da live do X' (de + artigo)."""
    for a, b in (("as ", "das "), ("os ", "dos "), ("a ", "da "), ("o ", "do ")):
        if noun.startswith(a):
            return b + noun[len(a):]
    return "de " + noun


def parse(raw: str | list[str]) -> dict[str, Any]:
    """Os pedaços com o papel de cada um, e o que dá para montar com eles."""
    terms = split_terms(raw)
    if not terms:
        raise KeywordError("Escreva pelo menos uma palavra-chave.")
    from app.radar import blocked_text
    from app.radar_hot import has_cta, unsafe

    roles = [{"text": ROLE_SUFFIX.sub("", t), "role": role_of(t), "forced": bool(ROLE_SUFFIX.search(t))} for t in terms]
    terms = [r["text"] for r in roles]
    words = [r["text"] for r in roles if r["role"] != "gancho"]
    if any(unsafe(t) for t in terms):
        raise KeywordError("Tem palavra proibida (menor de idade, sem consentimento ou parente): sem frases.")
    if any(has_cta(t) for t in terms):
        raise KeywordError("Tire link, bio, grupo, Telegram, privado ou venda: as frases vivem só de engajamento.")
    if any(blocked_text(t) for t in words):
        raise KeywordError("Assunto sensível (morte, tragédia, crime, doença ou criança): sem frases.")
    names = [r["text"] for r in roles if r["role"] == "nome"]
    if names and any(SEX_RE.search(src.norm(t)) for t in terms):
        raise KeywordError("Com nome de pessoa real o gerador não usa palavra sexual sobre ela. Tire a palavra ou o nome.")
    if not words:
        raise KeywordError("Junte ao gancho o assunto: um nome, um fato (live, foto, treta…) ou um tema.")
    hooks = [HOOKS[src.norm(r["text"])] for r in roles if r["role"] == "gancho"]
    facts = [FACTS[src.norm(r["text"])] for r in roles if r["role"] == "fato"]
    themes = [r["text"].lower() if not r["text"][:2].isupper() else r["text"] for r in roles if r["role"] == "tema"]
    return {"terms": terms, "roles": roles, "names": names, "hooks": hooks, "facts": facts, "themes": themes}


# ---- Montagem ----

def event_parts(names: list[str], fact: tuple[str, str] | None) -> tuple[str, str] | None:
    """(manchete sem artigo, o acontecimento com artigo): "Pitbull faz live com Elisa" / "a live do Pitbull com a Elisa"."""
    a = names[0] if names else None
    b = names[1] if len(names) > 1 else None
    if fact:
        verb, noun = fact
        if a and b:
            return f"{a} {verb} com {b}", f"{noun} {de(a)} com {art(b)}"
        if a:
            return f"{a} {verb}", f"{noun} {de(a)}"
        return None
    if a and b:
        return f"{a} e {b} viram assunto", f"esse lance {de(a)} com {art(b)}"
    if a:
        return f"{a} vira assunto", f"esse assunto {de(a)}"
    return None


EVENT_T = {
    "manchete": (
        "{H}!!\n\n{S}", "{H}!! {S} {E}", "{S}. {H}!! {E}", "{H}!!\n\n{S}\n\nVocês já viram? 👀", "Acabou de acontecer: {S} {E}",
        "{H}!!\n\n{S} e a internet não tá preparada", "{H}!!\n\n{S}\n\nAgora entendi tudo 👀", "{S}\n\n{H}!! 🔥",
        "{H}!! {S}. Ninguém esperava essa", "{H}!!\n\n{S}\n\nO que vocês acham?",
    ),
    "pergunta": (
        "Vocês viram {N}? {C}", "O que vocês acharam {dN}? {C}", "Alguém mais viu {N} ou só eu? 👀",
        "Quem assistiu {N} até o fim? {C}", "Vocês esperavam {N}? {C}", "Sinceramente: {N} foi armação ou foi real? {C}",
        "Homens, o que vocês acharam {dN}? {C}", "Me contem: vale a pena ver {N}? {C}", "{H}!! Vocês viram {N}? {C}",
        "Por que ninguém tá falando {dN}? 🤔", "Quem aqui já sabia {dN}? {C}",
    ),
    "polemica": (
        "Vou falar o que ninguém tem coragem: {N} foi muito pior do que parece. Pode me xingar {E}",
        "Opinião impopular sobre {N}: eu achei pouco 😏", "Todo mundo falando {dN} e ninguém percebeu o principal 👀",
        "Vão me cancelar, mas {N} não me surpreendeu nem um pouco", "{Ncap} dividiu a internet. De que lado você tá? {E}",
        "Quem defende {N} vai ter que me explicar uma coisa 😏", "Ou você ama ou você odeia {N}. Não tem meio-termo",
        "{Ncap} foi a melhor coisa da semana e eu defendo isso até o fim {E}", "Ninguém tem coragem de falar, mas {N} foi pura armação. Ou não? 🤔",
    ),
    "enquete": (
        "{Ncap}: aprovado ou reprovado? {E}", "{Ncap}: vocês assistiram ou fingiram que não viram? 😏",
        "{Ncap}: hype ou flop? Responde sem pensar", "Nota de 0 a 10 pra {N}? {C}", "{Ncap}: vocês querem mais ou chega? 👀",
        "{Ncap}: valeu a pena ou foi perda de tempo? {E}",
    ),
    "enquete2": (
        "{A} ou {B}: quem saiu ganhando nessa? {E}", "Time {A} ou time {B}? {E}", "{A} ou {B}: quem tá certo? Defende o seu nos comentários",
    ),
    "desafio": (
        "Duvido você não ter assistido {N} escondido 😏", "Aposto que metade de vocês já viu {N} duas vezes",
        "Duvido alguém aqui defender {N} com argumento rs", "Aposto que você vai comentar sobre {N} e depois apagar 👀",
        "Duvido você ler isso e não ir procurar {N} 😏",
    ),
    "urgencia": (
        "Se você ainda não viu {N}, você tá atrasado {E}", "Quem não viu {N} perdeu o assunto da semana",
        "Corre que {N} tá rodando em todo lugar 👀", "Ninguém fala de outra coisa hoje: {N} {E}",
        "{H}!! Corre que {N} tá rodando em todo lugar 👀",
    ),
    "confissao": (
        "Confesso: vi {N} umas três vezes 🙈", "Confissão: passei a noite vendo {N} e não me arrependo",
        "Admito que {N} me deixou curiosa 👀 Quem mais?", "Confesso que eu já sabia {dN} antes de todo mundo rs",
    ),
    "promessa": (
        "Se esse post bater {n} {unit} eu conto o que achei {dN} {E}", "Já que todo mundo tá falando {dN}: {n} {unit} e eu {R} {E}",
        "Se {N} virar trend hoje eu {R} {E}", "{H}!! Se esse post bater {n} {unit} eu {R} {E}",
    ),
}
THEME_T = {
    "pergunta": (
        "Pergunta séria: o que vocês pensam quando o assunto é {t}? {C}", "Quem aqui também é viciado em {t}? 👀",
        "Sejam sinceros: {t} é tudo isso mesmo? {C}", "Homens, {t} chama atenção de vocês ou não? {C}",
        "Qual a primeira coisa que vem na cabeça quando eu falo {t}? {C}",
    ),
    "enquete": ("{T}: ama ou odeia? {E}", "{T}: sim ou com certeza? rs", "{T}: aprovado ou reprovado? {E}"),
    "enquete2": ("{T} ou {t2}? {C}", "Só pode escolher um: {t} ou {t2}? {E}", "{T} ou {t2}? Defende o seu nos comentários"),
    "polemica": (
        "Opinião impopular sobre {t}: quem fala que não liga tá mentindo 😏", "Vão me cancelar, mas {t} é a melhor coisa que existe",
        "Ninguém tem coragem de falar a verdade sobre {t}. Eu tenho {E}", "{T} divide qualquer conversa. De que lado você tá?",
    ),
    "desafio": ("Duvido você passar um dia sem pensar em {t} rs", "Aposto que você não aguenta 5 minutos falando de {t} comigo 😏",
                "Duvido alguém aqui me explicar {t} em uma frase {E}"),
    "promessa": ("Se esse post bater {n} {unit} eu posto uma foto de {t} {E}", "Se esse post bater {n} {unit} eu conto tudo sobre {t} {E}",
                 "{n} {unit} e eu {R}. Assunto de hoje: {t} {E}"),
    "confissao": ("Confissão: eu penso em {t} mais do que deveria 🙈", "Admito que {t} me tira o foco o dia inteiro 👀 Quem mais?",
                  "Confesso: {t} é meu ponto fraco rs"),
    "urgencia": ("Quem tá falando de {t} hoje? Me conta que eu leio tudo 👀", "Hoje o assunto é {t} e ninguém me segura {E}"),
    "duplo": ("Hoje eu só quero {t}… e o resto que vier junto rs", "Tô precisando de {t}… pensou no quê? 😏",
              "Me chama pra falar de {t} que eu vou longe… na conversa, claro rs"),
}
# Quente: mais formatos sobre quem posta (nunca sobre a pessoa do nome).
THEME_Q = {
    "pergunta": ("Homens, {t} deixa vocês com vontade ou é só comigo? 😈", "O que vocês fariam comigo depois de {t}? {C}"),
    "confissao": ("Confesso: {t} me deixa com um fogo que vocês nem imaginam 😈", "Ninguém sabe, mas {t} me deixa molhadinha 🙈"),
    "duplo": ("Depois de {t} eu fico sem energia pra nada… quase nada 😏", "Com {t} eu aguento a noite inteira… conversando, claro rs"),
}


def _fill(template: str, rng: random.Random, **slots: str) -> str:
    n, unit = rng.choice(GOALS)
    values = {"E": rng.choice(EMOJI), "C": rng.choice(CLOSERS), "n": str(n), "unit": unit} | slots
    return template.format(**values)


def _makers(info: dict[str, Any], heat: str) -> list[tuple[str, Any]]:
    """(formato, função(rng) → frase) para tudo o que dá para montar com essas palavras."""
    names, facts, themes, hooks = info["names"], info["facts"], info["themes"], info["hooks"]
    rewards = REWARDS if heat == "quente" else LEVE_REWARDS
    makers: list[tuple[str, Any]] = []
    events = [event_parts(names, fact) for fact in (facts or [None])]
    if names and len(names) > 2:  # dois a dois também
        events += [event_parts([names[i], names[j]], facts[0] if facts else None)
                   for i in range(len(names)) for j in range(len(names)) if i != j][:4]
    events = [e for e in events if e]
    if not events and facts:  # só o fato ("live, vazou"): o fato vira o assunto
        noun = facts[0][1]
        events = [(f"{_cap(noun)} que todo mundo tá comentando", f"{noun} que todo mundo tá comentando")]
    for sentence, noun in events:
        for fmt, templates in EVENT_T.items():
            if fmt == "enquete2" and len(names) < 2:
                continue
            for template in templates:
                def make(rng, template=template, sentence=sentence, noun=noun):
                    hook = rng.choice(hooks) if hooks and rng.random() < .8 else rng.choice(DEFAULT_HOOKS)
                    line = _fill(template, rng, H=hook, S=sentence, N=noun, Ncap=_cap(noun), dN=d(noun),
                                 A=names[0] if names else "", B=names[1] if len(names) > 1 else "", R=rng.choice(rewards))
                    if hooks and "{H}" not in template and rng.random() < .3 and not line.startswith(tuple(hooks)):
                        line = f"{rng.choice(hooks)}!! {line}"  # o gancho que você deu também abre outras frases
                    return line
                makers.append(("enquete" if fmt == "enquete2" else fmt, make))
    for i, theme in enumerate(themes):
        other = [t for t in themes if t != theme]
        pools = dict(THEME_T)
        if heat == "quente" and not names:  # tom quente só sem nome real na frase
            pools = {k: v + THEME_Q.get(k, ()) for k, v in THEME_T.items()}
        for fmt, templates in pools.items():
            if fmt == "enquete2" and not other:
                continue
            if fmt == "duplo" and heat != "quente":
                continue
            for template in templates:
                def make(rng, template=template, theme=theme, other=other):
                    t2 = rng.choice(other) if other else ""
                    return _fill(template, rng, t=theme, T=_cap(theme), t2=t2, R=rng.choice(rewards))
                makers.append(("enquete" if fmt == "enquete2" else fmt, make))
    return makers


def _related(info: dict[str, Any], heat: str, mix: int, day: int) -> list[tuple[str, str]]:
    """Do gerador quente: frases que já falam dos seus temas (lingerie, cama, academia…). Só sem nome real."""
    if heat != "quente" or info["names"] or not info["themes"]:
        return []
    from app.radar_hot import format_of

    words = {w for t in info["themes"] for w in src.norm(t).split() if len(w) >= 4}
    mapping = {"provocacao": "duplo", "lista": "confissao"}
    out = []
    for line in _compose(mix, 1200, day, None):
        if words & set(src.norm(line).split()):
            fmt = format_of(line)
            out.append((mapping.get(fmt, fmt), line))
    return out


def bank(raw: str | list[str], heat: str = "quente", mix: int = 0, now: datetime | None = None) -> list[tuple[str, str]]:
    terms = split_terms(raw)
    parse(terms)  # recusa antes de montar
    day = (now or now_sp()).weekday()
    return list(_bank(tuple(terms), heat, mix, day, now))


@lru_cache(maxsize=64)
def _bank(terms: tuple[str, ...], heat: str, mix: int, day: int, now: datetime | None) -> tuple[tuple[str, str], ...]:
    from app.radar_hot import has_cta, unsafe

    info = parse(list(terms))
    makers = _makers(info, heat)
    rng = random.Random(f"kw|{'|'.join(src.norm(t) for t in terms)}|{heat}|{mix}")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(fmt: str, line: str) -> None:
        line = "\n".join(" ".join(part.split()) for part in line.split("\n"))
        key = src.norm(line)  # sem emoji nem pontuação: "…? 👀" e "…? 🔥" são a mesma frase
        sexual = bool(SEX_RE.search(key))
        if (key in seen or len(line) > MAX_LEN or unsafe(line) or has_cta(line) or not fits_day(line, now)
                # com nome real: nada sexual fora da promessa de quem posta, e nunca "VAZOU!!" junto de prêmio
                # sexual (leria como conteúdo íntimo da pessoa vazado)
                or (info["names"] and sexual and (fmt != "promessa" or HOOK_START.match(line)))):
            return
        seen.add(key)
        out.append((fmt, line))

    for fmt, line in _related(info, heat, mix, day):
        add(fmt, line)
    if makers:
        for _ in range(POOL * 6):
            if len(out) >= POOL:
                break
            fmt, make = rng.choice(makers)
            add(fmt, make(rng))
    return tuple(out)


# ---- Nota de potencial viral ----

def viral_score(line: str, fmt: str, terms: list[str], potential: dict[str, Any] | None = None) -> int:
    """0–100, sem API: o quanto a frase puxa resposta (o que o X mais entrega). Pesa o formato, pergunta,
    chamada direta, polêmica, gancho em caixa alta, tamanho, quebra de linha, emoji, se usa as suas palavras e,
    com potential, o formato que mais rende nas suas contas e no nicho."""
    from app.radar_hot import format_of

    key = src.norm(line)
    score = FMT_BASE.get(fmt, 22)
    if "?" in line:
        score += 12
    if ADDRESS_RE.search(key):
        score += 6
    if POLAR_RE.search(key):
        score += 8
    if HOOK_START.match(line):
        score += 7
    size = len(line)
    score += 10 if 50 <= size <= 160 else 5 if size <= 220 else -6
    if size < 35:
        score -= 6
    if "\n" in line:
        score += 4
    emojis = len(re.findall(r"[\U0001F300-\U0001FAFF]", line))
    score += 3 if 1 <= emojis <= 2 else -4 if emojis > 2 else 0
    words = [src.norm(t) for t in terms if src.norm(t)]
    if words:
        used = sum(1 for w in words if w in key or w.split()[0] in key.split())
        score += round(14 * used / len(words))
    if potential:
        best = max((p.get("score") or 0) for p in potential.values()) or 1
        mine = (potential.get(format_of(line)) or {}).get("score") or 0
        score += round(10 * mine / best)
    return max(1, min(99, score))


def tier_of(score: int) -> tuple[str, str]:
    if score >= 80:
        return "alto", "Muito alto"
    if score >= 65:
        return "bom", "Alto"
    if score >= 50:
        return "medio", "Médio"
    return "baixo", "Baixo"


def ordered(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Melhor nota primeiro, mas cada página de 12 com no máximo 3 do mesmo formato, 5 abrindo com o gancho em
    caixa alta e nunca a mesma frase só com outro gancho (variedade)."""
    rest = sorted(items, key=lambda i: (-i["score"], i["text"]))
    out: list[dict[str, Any]] = []
    while rest:
        page: list[dict[str, Any]] = []
        count: dict[str, int] = {}
        shapes: set[str] = set()  # a mesma frase só trocando o gancho ("EITA!!"/"BABADO!!") fica para outra página

        def shape(item: dict[str, Any]) -> str:
            return src.norm(HOOK_START.sub("", item["text"]))

        for fmt in dict.fromkeys(i["fmt"] for i in rest):  # a melhor de cada formato abre a página
            if len(page) >= PAGE:
                break
            full = count.get("!!", 0) >= 5
            item = next((i for i in rest if i["fmt"] == fmt and not (full and HOOK_START.match(i["text"]))
                         and shape(i) not in shapes), None)
            if item is None:
                continue
            hooked = bool(HOOK_START.match(item["text"]))
            page.append(item)
            shapes.add(shape(item))
            count[fmt] = 1
            count["!!"] = count.get("!!", 0) + hooked
            rest.remove(item)
        for item in list(rest):
            if len(page) >= PAGE:
                break
            hooked = bool(HOOK_START.match(item["text"]))
            if count.get(item["fmt"], 0) < 3 and (not hooked or count.get("!!", 0) < 5) and shape(item) not in shapes:
                page.append(item)
                shapes.add(shape(item))
                count[item["fmt"]] = count.get(item["fmt"], 0) + 1
                count["!!"] = count.get("!!", 0) + hooked
                rest.remove(item)
        for item in list(rest):  # sobrou formato demais: completa a página
            if len(page) >= PAGE:
                break
            page.append(item)
            rest.remove(item)
        out += sorted(page, key=lambda i: -i["score"])
    return out


def generate(raw: str | list[str], heat: str = "quente", page: int = 0, mix: int = 0, fmt: str | None = None,
             potential: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    info = parse(raw)
    return _paged(bank(raw, heat, mix, now), info["roles"], info["terms"], heat, page, mix, fmt, potential)


def _paged(lines: list[tuple[str, str]], roles: list[dict[str, Any]], terms: list[str], heat: str, page: int, mix: int,
           fmt: str | None, potential: dict[str, Any] | None) -> dict[str, Any]:
    formats: dict[str, int] = {}
    for f, _ in lines:
        formats[f] = formats.get(f, 0) + 1
    chosen = [(f, line) for f, line in lines if not fmt or f == fmt]
    items = ordered([{"text": line, "fmt": f, "fmt_label": FORMATS.get(f, f),
                      "score": viral_score(line, f, terms, potential)} for f, line in chosen])
    for item in items:
        item["tier"], item["tier_label"] = tier_of(item["score"])
    pages = max(1, -(-len(items) // PAGE))
    start = page * PAGE
    return {"terms": roles, "heat": heat, "mix": mix, "fmt": fmt, "page": page, "pages": pages,
            "total": len(items), "done": start + PAGE >= len(items), "formats": formats,
            "format_labels": FORMATS, "items": items[start:start + PAGE]}


def grok_prompt(raw: str | list[str], heat: str = "quente", refs: list[str] | None = None, phrase: str | None = None) -> str:
    """Turbo com Grok: as mesmas palavras (ou a mesma frase pronta), todos os formatos, as mesmas regras."""
    from app.radar_ideas import FORMAT_GUIDE, OWNER_EXAMPLES_LEVE, OWNER_EXAMPLES_QUENTE

    info = read_phrase(phrase) if phrase else parse(raw)
    roles = ", ".join(f"{r['text']} ({r['role']})" for r in info["roles"]) or "as da frase"
    owner = OWNER_EXAMPLES_QUENTE if heat == "quente" else OWNER_EXAMPLES_LEVE
    lines = [
        "Você é a melhor copywriter de contas de texto no X, em português do Brasil, com gírias de internet. Quem posta é "
        "uma mulher; o público é de homens. O objetivo é o máximo de viralização: o algoritmo do X entrega o post que "
        "gera respostas, citações e discussão.",
        f"Palavras-chave que TODO post precisa usar (com o papel de cada uma): {roles}.",
        "Exemplo de manchete boa com palavras assim: \"VAZOU!!\\n\\nPitbull amigo do Fejuca faz live com Elisa Sanches\".",
        "Escreva 10 posts, cada um num formato diferente: manchete de fofoca com gancho em caixa alta, pergunta direta, "
        "polêmica (opinião que divide), enquete de duas opções, desafio ao ego, promessa condicional, confissão, "
        "urgência (quem não viu perdeu) e duplo sentido.",
        FORMAT_GUIDE,
        "Tom do dono: " + " | ".join(f'"{e}"' for e in owner),
    ]
    if phrase:
        lines.insert(2, f"Frase base do dono: \"{info['phrase']}\". Todo post conta ESSA mesma notícia: o mesmo fato, os "
                        "mesmos nomes e o mesmo sentido; muda só o jeito de contar (o formato). Nada solto nem genérico.")
    if info["names"]:
        lines.append("Os nomes são pessoas reais: fale só do fato que as palavras dizem, sem inventar fato novo, sem nada "
                     "sexual sobre elas e sem dizer que vazou conteúdo íntimo delas.")
    if refs:
        lines.append("Textos que já engajaram de verdade (referência, não copie): " + " | ".join(refs))
    lines += [rules(heat), "Um post por linha (troque quebra de linha dentro do post por \" / \"), sem numeração nem aspas, "
              "até 220 caracteres. Não explique nada."]
    return "\n".join(lines)


# ---- Frase pronta: a frase inteira entra e o gerador separa as partes sozinho ----
# "VAZOU!! Pitbull amigo do Fejuca faz live com Elisa Sanches" → gancho VAZOU, quem = "Pitbull amigo do Fejuca",
# fato = "faz live" (o verbo é o seu), com quem = "Elisa Sanches". As frases novas usam a sua frase inteira ou a
# mesma notícia reescrita ("a live do Pitbull amigo do Fejuca com a Elisa Sanches"): nada de molde solto.
CONNECT = {"com", "contra", "e", "x", "vs", "versus", "pra", "para", "ao", "junto", "a", "o"}
NAME_GLUE = {"do", "da", "de", "dos", "das", "amigo", "amiga", "ex", "mae", "pai", "namorado", "namorada", "marido",
             "esposa", "irmao", "parceiro", "parceira", "filho", "filha"}
LEAD_HOOK = re.compile(r"^([A-ZÀ-Ú][A-ZÀ-Ú ]{1,24}?)\s*(?:!+|:)\s*")
TAIL_HOOK = re.compile(r"\s+([A-ZÀ-Ú]{3,}(?: [A-ZÀ-Ú]{2,})*)\s*!+\s*$")
TRAIL = re.compile(r"(?:[\s.!?…,;:]|[\U0001F300-\U0001FAFF☀-➿️])+$")
PUNCT = ",.;:!?…\"'"
EMOJI_AFTER_DOT = re.compile(r"\.\s+([\U0001F300-\U0001FAFF])")
PHRASE_T = {
    "manchete": ("{H}!!\n\n{S}", "{H}!! {S} {E}", "{S}\n\n{H}!! {E}", "{H}!!\n\n{S}\n\nVocês já viram? 👀",
                 "{H}!!\n\n{S}\n\nO que vocês acham?", "{H}!!\n\n{S}\n\nAgora entendi tudo 👀", "{H}!!\n\n{S}\n\nNinguém esperava essa"),
    "pergunta": ("{S}?? Isso é real? 👀", "Gente, {s}. Vocês já sabiam? {C}", "{S}. O que vocês acham disso? {C}",
                 "{S}. Alguém mais viu ou só eu? 👀", "{H}!! {S}. Vocês viram? {C}", "Homens, {s}. O que vocês acharam? {C}"),
    "polemica": ("{S}. Opinião impopular: já era esperado 😏", "{S} e a internet tá dividida. De que lado você tá? {E}",
                 "{S}. Vou falar o que ninguém tem coragem: eu achei pouco 😏", "{S}. Quem defende vai ter que me explicar uma coisa 😏"),
    "enquete": ("{S}. Aprovado ou reprovado? {E}", "{S}. Hype ou flop? Responde sem pensar", "{S}. Vocês assistiriam? Sim ou não? 👀",
                "{S}. Nota de 0 a 10? {C}"),
    "desafio": ("{S}. Duvido você não ir procurar agora 😏", "{S}. Aposto que metade de vocês já sabia rs"),
    "urgencia": ("Corre: {s} {E}", "Acabou de acontecer: {s} {E}", "{S}. Quem não viu ainda vai se arrepender 👀"),
    "confissao": ("{S}. Confesso que fiquei curiosa 👀 Quem mais?", "{S}. Admito: não esperava por essa 🙈"),
    "promessa": ("{S}. Se esse post bater {n} {unit} eu conto o que achei {E}", "{S}. {n} {unit} e eu {R} {E}"),
}
# Frase de quem posta (não é notícia): continua sendo dela, com um gancho de resposta no fim.
PERSONAL_T = {
    "pergunta": ("{S}. E vocês? {C}", "{S}… vocês fariam o quê? {C}", "Sinceramente: {s}. Vocês também? {C}",
                 "Homens, {s}. Isso é normal? {C}", "{S}. Me conta nos comentários 👀"),
    "confissao": ("Confissão: {s} 🙈", "Admito: {s}. Quem mais?", "Ninguém sabe, mas {s} 🤭 Vocês me julgam?"),
    "desafio": ("{S}. Duvido alguém aqui admitir o mesmo 😏", "{S}. Aposto que metade de vocês também rs"),
    "enquete": ("{S}. Certo ou errado? Responde sem pensar", "{S}. Normal ou exagero? {E}"),
    "promessa": ("{S}. Se esse post bater {n} {unit} eu {R} {E}", "{S}. {n} {unit} e eu {R} {E}"),
    "polemica": ("{S}. Pode me julgar, eu não ligo 😏", "Opinião impopular: {s}. Quem concorda?"),
}
QUESTION_T = {
    "pergunta": ("{S}? {C}", "Pergunta séria: {s}? {C}", "Homens, {s}? {C}", "Me respondam uma coisa: {s}? {C}",
                 "Preciso saber: {s}? {C}"),
    "enquete": ("{S}? Sim ou não? Responde sem pensar", "{S}? Um só, sem mentir {E}"),
    "desafio": ("{S}? Duvido alguém responder com sinceridade 😏",),
    "promessa": ("{S}? Se esse post bater {n} {unit} eu respondo a minha também {E}", "{S}? {n} {unit} e eu {R} {E}"),
}
# Frase que já é promessa com nome real e prêmio quente ("se o Lula ganhar eu mando nude"): só variações dela,
# sem gancho em caixa alta.
PROMISE_OPENERS = ("", "Homens,", "Vocês homens,", "Atenção, homens:", "Recado pros homens:", "Anota aí:", "Prometo:",
                   "Valendo:", "Já avisei:", "Tá valendo:")
PROMISE_CLOSERS = ("Combinado? 😈", "Quem vai torcer comigo? 👀", "Promessa é dívida 😈", "Comenta aí quem vai cobrar 👀",
                   "E eu cumpro, hein rs", "Torcendo muito? 😏", "🔥", "😈", "👀", "Quem tá dentro?")
PROMISE_T = tuple(f"{o} {{s}}. {k}".strip() if o else f"{{S}}. {k}" for o in PROMISE_OPENERS for k in PROMISE_CLOSERS)


# Palavra comum que só tem maiúscula por abrir a frase ("A live…", "Se o Lula…"): não é nome.
COMMON = {"a", "o", "as", "os", "se", "eu", "hoje", "um", "uma", "quando", "depois", "agora", "ontem", "gente", "voces",
          "voce", "nao", "sim", "so", "ja", "que", "quem", "mas", "e", "ai", "olha", "tipo", "cara", "mano", "esse", "essa",
          "isso", "aquele", "aquela", "meu", "minha", "nossa", "todo", "toda", "ninguem", "alguem", "homens", "por", "pra"}


def _is_cap(word: str) -> bool:
    word = word.strip(PUNCT)
    return word[:1].isupper() and src.norm(word) not in COMMON


def _name_span(tokens: list[str], start: int) -> list[str]:
    """Nome a partir de start: palavras com maiúscula, com "do/da/amigo…" só no meio ("Pitbull amigo do Fejuca")."""
    out: list[str] = []
    i = start
    while i < len(tokens):
        word = tokens[i].strip(PUNCT)
        if _is_cap(word):
            out.append(word)
        elif src.norm(word) in NAME_GLUE and any(_is_cap(t) for t in tokens[i + 1:i + 3]):
            out.append(word)
        else:
            break
        if re.search(r"[,.;:!?…]$", tokens[i]):  # pontuação colada acaba o nome
            break
        i += 1
    while out and not _is_cap(out[-1]):
        out.pop()
    return out


def _teams() -> set[str]:
    from app.radar_ideas import TEAMS

    return set(TEAMS)


def read_phrase(text: str) -> dict[str, Any]:
    """A frase pronta separada em partes: gancho, quem, fato (com o seu verbo), com quem e a frase-base."""
    from app.radar import blocked_text
    from app.radar_hot import format_of, has_cta, unsafe

    raw = " ".join((text or "").split())
    if len(src.norm(raw).split()) < 3:
        raise KeywordError("Escreva a frase inteira (ex.: VAZOU!! Pitbull amigo do Fejuca faz live com Elisa Sanches).")
    if len(raw) > 280:
        raise KeywordError("Frase longa demais: até 280 caracteres.")
    if unsafe(raw):
        raise KeywordError("Tem palavra proibida (menor de idade, sem consentimento ou parente): sem frases.")
    if has_cta(raw):
        raise KeywordError("Tire link, bio, grupo, Telegram, privado ou venda: as frases vivem só de engajamento.")
    hook, core = None, raw
    lead = LEAD_HOOK.match(core)
    loose = re.match(r"^([^\W\d_]+(?: [^\W\d_]+)?)\s*(?:!+|:)\s*", core)  # "vazou! …" em minúscula também é gancho
    if lead and (src.norm(lead.group(1)) in HOOKS or len(lead.group(1).strip()) >= 3):
        hook, core = HOOKS.get(src.norm(lead.group(1)), lead.group(1).strip()), core[lead.end():]
    elif loose and src.norm(loose.group(1)) in HOOKS:
        hook, core = HOOKS[src.norm(loose.group(1))], core[loose.end():]
    else:
        tail = TAIL_HOOK.search(core)
        if tail and src.norm(tail.group(1)) in HOOKS:
            hook, core = HOOKS[src.norm(tail.group(1))], core[:tail.start()]
    question = re.search(r"\?[\s\W]*$", core) is not None
    core = _cap(TRAIL.sub("", core).strip())  # depois do gancho a frase começa com maiúscula
    if blocked_text(core):
        raise KeywordError("Assunto sensível (morte, tragédia, crime, doença ou criança): sem frases.")
    if len(src.norm(core).split()) < 2:
        raise KeywordError("Junte ao gancho o assunto da frase (quem, o que aconteceu).")
    tokens = core.split()
    norms = [src.norm(t) for t in tokens]
    a = b = verb = noun = None
    conn = ""
    fact_at = next((i for i, n in enumerate(norms) if i > 0 and n in FACTS), None)
    if fact_at is not None:
        j = fact_at - 1
        while j >= 0 and not _is_cap(tokens[j]):
            j -= 1
        start = j
        while start > 0 and (_is_cap(tokens[start - 1]) or norms[start - 1] in NAME_GLUE):
            start -= 1
        while start <= j and not _is_cap(tokens[start]):
            start += 1
        span = _name_span(tokens, start) if j >= 0 else []
        if span and start + len(span) == j + 1:
            a = " ".join(span)
            verb = " ".join(tokens[j + 1:fact_at + 1]).strip(PUNCT)
            noun = FACTS[norms[fact_at]][1]
            k = fact_at + 1
            while k < len(tokens) and norms[k] in CONNECT and not _is_cap(tokens[k]):
                conn = conn or norms[k]
                k += 1
            if k < len(tokens) and conn:
                b = " ".join(_name_span(tokens, k)) or None
    names = [n for n in (a, b) if n]
    if not names:  # sem fato: os nomes da frase (maiúscula fora do começo, ou nome de 2+ palavras)
        i = 0
        while i < len(tokens):
            span = _name_span(tokens, i) if _is_cap(tokens[i]) else []
            if span and (i > 0 or len(span) > 1):
                names.append(" ".join(span))
                i += len(span)
            else:
                i += 1
    people = [n for n in names if src.norm(n) not in _teams()]
    sexual = bool(SEX_RE.search(src.norm(core)))
    if people and sexual and format_of(raw) != "promessa":
        raise KeywordError("Com nome de pessoa real o gerador não usa palavra sexual sobre ela. Tire a palavra ou o nome.")
    roles = [{"text": hook, "role": "gancho", "forced": False}] if hook else []
    roles += [{"text": n, "role": "nome", "forced": True} for n in names]
    if a:
        roles.append({"text": tokens[fact_at].strip(PUNCT), "role": "fato", "forced": False})
    event = None
    if noun and a:
        event = f"{noun} {de(a)}" + (f" {'contra' if conn == 'contra' else 'com'} {art(b)}" if b else "")
    news = hook or event or (any(n in FACTS for n in norms) and "eu" not in norms)  # cita live/foto/treta… e não é "eu"
    kind = ("noticia" if news else "promessa" if format_of(raw) == "promessa" else "pergunta" if question
            else "pessoal")
    return {"phrase": raw, "hook": hook, "sentence": core, "question": question, "event": event, "names": names, "kind": kind,
            "people": people, "verb": verb, "promise_only": bool(people and sexual), "roles": roles,
            "terms": [r["text"] for r in roles if r["role"] != "gancho"] or [core]}


def phrase_bank(text: str, heat: str = "quente", mix: int = 0, now: datetime | None = None) -> list[tuple[str, str]]:
    read_phrase(text)  # recusa antes de montar
    day = (now or now_sp()).weekday()
    return list(_phrase_bank(" ".join((text or "").split()), heat, mix, day, now))


@lru_cache(maxsize=64)
def _phrase_bank(text: str, heat: str, mix: int, day: int, now: datetime | None) -> tuple[tuple[str, str], ...]:
    from app.radar_hot import format_of, has_cta, unsafe
    from app.radar_ideas import variations

    info = read_phrase(text)
    rewards = REWARDS if heat == "quente" and not info["people"] else LEVE_REWARDS
    sentence, event = info["sentence"], info["event"]
    hooks = [info["hook"]] if info["hook"] else []
    rng = random.Random(f"frase|{src.norm(text)}|{heat}|{mix}")
    out: list[tuple[str, str]] = []
    seen = {src.norm(text)}

    def add(fmt: str, line: str) -> None:
        line = "\n".join(" ".join(part.split()) for part in line.split("\n"))
        line = EMOJI_AFTER_DOT.sub(r" \1", line)  # "…vocês. 🔥" → "…vocês 🔥"
        key = src.norm(line)
        sexual = bool(SEX_RE.search(key))
        if (key in seen or len(line) > MAX_LEN or unsafe(line) or has_cta(line) or not fits_day(line, now)
                or (info["people"] and sexual and (not info["promise_only"] or HOOK_START.match(line)))):
            return
        seen.add(key)
        out.append((fmt, line))

    for line in variations(info["phrase"], 12, seed=mix):  # a própria frase com abertura, fecho e meta trocados
        fmt = format_of(line)
        add({"provocacao": "manchete", "lista": "confissao"}.get(fmt, fmt), line)
    kind = info["kind"]
    if info["promise_only"] or kind == "promessa":
        pools: dict[str, list[str]] = {"promessa": list(PROMISE_T)}
    elif kind == "pergunta":
        pools = {fmt: list(t) for fmt, t in QUESTION_T.items()}
    elif kind == "pessoal":
        pools = {fmt: list(t) for fmt, t in PERSONAL_T.items()}
    else:
        pools = {fmt: list(t) for fmt, t in PHRASE_T.items()}
        if event:  # a mesma notícia reescrita com artigo: libera os moldes de acontecimento
            for fmt, templates in EVENT_T.items():
                if fmt == "enquete2":
                    if len(info["names"]) >= 2:
                        pools["enquete"].extend(templates)
                    continue
                # só moldes que carregam a notícia ({N}); a manchete crua ({S}) já vem de PHRASE_T
                pools.setdefault(fmt, []).extend(t for t in templates if "{S}" not in t and re.search(r"\{(?:N|Ncap|dN)\}", t))
    makers = [(fmt, t) for fmt, templates in pools.items() for t in templates]
    a, b = (info["names"] + ["", ""])[:2]
    for _ in range(POOL * 4):
        if len(out) >= POOL:
            break
        fmt, template = rng.choice(makers)
        hook = rng.choice(hooks) if hooks and rng.random() < .85 else rng.choice(DEFAULT_HOOKS)
        noun = event or sentence
        line = _fill(template, rng, H=hook, S=sentence, s=_low(sentence), N=noun, Ncap=_cap(noun),
                     dN=d(event) if event else "disso", A=a, B=b, R=rng.choice(rewards))
        if hooks and "{H}" not in template and rng.random() < .3 and kind == "noticia":
            line = f"{hooks[0]}!! {line}"
        add(fmt, line)
    return tuple(out)


def generate_phrase(text: str, heat: str = "quente", page: int = 0, mix: int = 0, fmt: str | None = None,
                    potential: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    info = read_phrase(text)
    lines = phrase_bank(text, heat, mix, now)
    return _paged(lines, info["roles"], info["terms"], heat, page, mix, fmt, potential) | {
        "phrase": {"hook": info["hook"], "sentence": info["sentence"], "event": info["event"], "kind": info["kind"]}}
