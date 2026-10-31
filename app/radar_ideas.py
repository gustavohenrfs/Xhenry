"""Ideias de post para contas de texto (modelos +18 no X, público masculino) a partir de um assunto do Radar.

Dois níveis: LEVE (o tom dos primeiros exemplos do dono: "se o Flamengo ganhar eu…", "quem tá vendo comigo?",
sugestivo) e QUENTE (o tom direto do nicho: promessa de nude, pergunta polêmica aos homens, desafio ao ego,
enquete, duplo sentido). Só engajamento viral: nunca chama para link, bio, grupo ou Telegram. Regras fixas em
qualquer nível, nos modelos e no pedido ao Grok:
- nunca sexualizar uma pessoa real com nome (o assunto é a condição ou o gancho; o conteúdo é sobre quem posta);
- nada com menor de idade, sem consentimento ou parentes; assunto bloqueado pelo radar não ganha ideia.

ideas() é grátis (modelos por tipo de assunto, sorteados por assunto e semente para não repetir);
ask_grok_lines() usa a chave xAI do usuário.
"""

from __future__ import annotations

import os
import random
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
# Jogo "A x B" sem um dos grandes no topo: enquete, que é o que mais gera comentário.
MATCH = [
    "{a} ou {b}? Comenta quem ganha hoje que o torcedor do vencedor ganha um presentinho meu 😏",
    "Se tiver gol em {topic} eu posto aquela foto que vocês pediram rs",
    "Quem vai ver {topic} comigo hoje? Prometo torcer de um jeito diferente rs",
    "Torcedor nervoso é o mais carinhoso, concordam? rs",
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
FIGHT = [
    "Se o brasileiro ganhar no {topic} eu solto uma foto que vocês vão gostar rs",
    "Homem que assiste {topic} até o fim tem minha atenção 👀",
    "Vendo {topic} e pensando que eu também sei ser agressiva… rs",
    "Quem vai ver {topic} comigo hoje? Comenta que eu respondo 😏",
]
SPORTS = [
    "Se o Brasil mandar bem em {topic} eu posto aquela foto rs",
    "Acordar cedo pra ver {topic}? Só se for comigo 😏",
    "Homem que entende de {topic} me ganha fácil rs",
    "Quem tá vendo {topic}? Me conta quem vai ganhar 👀",
]
GAMES = [
    "Se vocês me chamarem pra jogar {topic} eu deixo vocês ganharem rs",
    "Gamer que joga {topic} até de madrugada… eu tô acordada também 😏",
    "Prefiro vocês jogando {topic} ou me dando atenção? Comenta 👀",
    "Quem zerar {topic} primeiro ganha um presentinho meu rs",
]
CARS = [
    "Homem que entende de {topic} tem minha atenção 👀",
    "Me leva pra dar uma volta? Prometo me comportar… ou não rs",
    "Todo mundo falando de {topic} e eu só querendo uma carona 😏",
    "Qual carro vocês têm? Me conta que eu digo se eu entro rs",
]
HANGOUT = [
    "{topic}! Quem vai pra resenha hoje? Me chama que eu levo a festa pra outro nível rs",
    "E eu aqui sem ninguém pra beber comigo… quem vem? 😏",
    "Homem que faz churrasco bem feito já tá na frente rs",
    "Se esse post bater 100 comentários hoje eu posto aquela foto 👀",
]
GENERIC = [
    "Todo mundo falando de {topic} e eu aqui pensando em outra coisa rs",
    "Alguém me explica {topic}? Em troca eu… rs",
    "{topic} nos trends e eu sem entender nada, me ensina? 👀",
    "Comenta {topic} que eu respondo do meu jeito 😏",
]

# ---- Nível QUENTE: o tom direto do nicho (promessa, pergunta polêmica aos homens, desafio, enquete, duplo
# sentido). O assunto é só a condição ou o gancho; o conteúdo é sobre quem posta ou sobre "os homens". ----
FOOTBALL_TEAM_Q = [
    "Se o {team} ganhar hoje eu mando nude pra quem comentar o placar 🔥",
    "Dizem que os {fans} são os mais safados na cama… alguém confirma? 😏",
    "{fans}, é verdade que vocês não aguentam nem o primeiro tempo? Sinceramente 👀",
    "Se o {team} fizer 2 gols eu posto sem nada por baixo. Combinado? rs",
    "Se o {team} perder hoje eu consolo os {fans} do meu jeito rs",
    "Torcida do {team} ou torcida rival: qual geme mais alto no gol? Defende a sua 👀",
]
MATCH_Q = [
    "{a} ou {b}? A torcida que ganhar ganha nude meu 🔥 Comenta o seu time",
    "Se tiver gol em {topic} eu tiro uma peça por gol. Quem tá vendo comigo? 😏",
    "Torcida de {a} ou de {b}: qual é a mais safada? Defende a sua aí 👀",
    "Homem que grita gol alto também geme alto? Pergunta séria, {topic} hoje rs",
    "Quem acertar o placar de {topic} ganha um nude, comenta aí 🔥",
]
FOOTBALL_Q = [
    "Se meu time ganhar hoje eu mando foto peladinha pra quem comentar 🔥",
    "{topic} hoje: vocês preferem ver o jogo ou me ver sem roupa? Sinceramente",
    "Homem que vê {topic} até o fim aguenta 90 minutos na cama também? 👀",
    "Quem acertar o placar de {topic} ganha um nude, comenta aí 😏",
    "Duvido um homem largar {topic} pra ficar comigo na cama rs",
]
POLITICS_Q = [
    "Homens, eu prometo um nude aqui se {topic} ganhar 🔥",
    "Se {topic} ganhar eu mando aquela foto que vocês nunca viram rs",
    "Homem de esquerda ou de direita: qual é melhor na cama? Briguem 👀",
    "Todo mundo brigando por causa de {topic} e eu só querendo um homem que dure mais que o debate rs",
    "Política me deixa nervosa… alguém vem me acalmar do jeito certo? 😏",
]
FIGHT_Q = [
    "Se o brasileiro ganhar no {topic} eu mando nude pra quem comentar 🔥",
    "Homem que aguenta 5 rounds aguenta quanto tempo na cama? Sinceramente 👀",
    "Por nocaute ou por pontos? Do jeito que eu gosto é por cansaço rs",
    "Vendo {topic} e pensando que eu também sei pegar pesado… quem aguenta? 😏",
]
SPORTS_Q = [
    "Se o Brasil mandar bem em {topic} eu posto sem roupa 🔥",
    "Homem atleta tem mais fôlego na cama ou é lenda? Me conta 👀",
    "Acordar cedo pra ver {topic} ou ficar na cama comigo? Escolham rs",
    "Duvido um homem que treina aguentar uma noite inteira comigo 😏",
]
GAMES_Q = [
    "Homem que joga {topic} a noite toda tem fôlego pra mim depois? 👀",
    "Se esse post bater 500 comentários eu jogo {topic} sem roupa rs",
    "Gamer goza rápido igual dá rage quit? Pergunta séria 😏",
    "{topic} ou eu de lingerie te esperando? Sinceramente, qual vocês escolhem?",
]
CARS_Q = [
    "Homem com carro rebaixado é melhor no banco de trás? Sinceramente 👀",
    "Me leva pra dar uma volta? Prometo que o banco de trás não fica limpo rs",
    "{topic} em alta e eu só querendo saber: vocês já transaram dentro do carro? 😏",
    "Carro bonito ou pegada boa? Só pode escolher um 🔥",
]
HANGOUT_Q = [
    "Resenha boa termina comigo sentando, concordam? rs",
    "{topic}! Quem vai beber comigo hoje? Bêbada eu fico muito carinhosa 😏",
    "Homem bêbado aguenta mais ou goza mais rápido? Me explica 👀",
    "Se esse post bater 100 comentários hoje eu posto aquela foto sem censura 🔥",
]
TV_Q = [
    "Se eu entrasse no {topic} vocês votavam em mim ou me levavam pro quarto? rs",
    "Assistindo {topic} sem nada por baixo… alguém mais? 👀",
    "Quem tá vendo {topic} comigo? Cada 10 comentários eu tiro uma peça 🔥",
    "Duvido algum homem prestar atenção em {topic} comigo do lado 😏",
]
MUSIC_Q = [
    "Ouvindo {topic} e com vontade de sentar… na cadeira, claro rs",
    "Música de {topic} pra transar: sim ou não? Sinceramente 👀",
    "Qual música de {topic} vocês colocariam pra me ver dançando sem roupa? 😏",
]
DATES_Q = [
    "Feliz {topic}! Se esse post bater 200 comentários eu posto o presente de vocês 🔥",
    "No {topic} eu vou ser bem safada… quem merece? 😏",
    "{topic} chegando: vocês preferem nude ou foto de lingerie? Sinceramente",
    "Quem vai passar o {topic} comigo? Prometo não deixar ninguém dormir rs",
]
# Pessoa real: a frase só usa o burburinho como gancho; nada sobre a pessoa.
PERSON_Q = [
    "Todo mundo falando de {topic} e eu aqui querendo saber: homem prefere peluda ou lisinha? 👀",
    "Enquanto vocês comentam {topic}, eu tô aqui sem calcinha esperando atenção rs",
    "{topic} nos trends e eu só pensando besteira… alguém me distrai? 😏",
    "Esquece {topic}: vocês homens gozam rápido mesmo ou é lenda? Sinceramente",
]
GENERIC_Q = [
    "Todo mundo falando de {topic} e eu só pensando em sentar rs",
    "Se {topic} continuar nos trends até amanhã eu posto nude 🔥",
    "{topic} em alta e eu com uma dúvida: vocês homens gozam rápido mesmo? Sinceramente 👀",
    "Quem me explicar {topic} direitinho ganha um nude, comenta 😏",
    "Duvido um homem comentar {topic} sem olhar minha foto primeiro rs",
]
# Sem assunto: perguntas quentes que servem a qualquer hora ("Pergunta quente").
EVERGREEN_Q = [
    "Os homens gostam de peluda ou lisinha? Tem alguém que diz que não faz diferença? Estou curiosa",
    "Por que os homens acham difícil continuar transando depois que gozam, vocês são fracos?",
    "Vocês homens gozam rápido mesmo ou é só falta de prática? Sinceramente",
    "Fantasias que toda mulher quer experimentar pelo menos uma vez. A primeira é…",
    "Homem que demora pra gozar é bom ou cansativo? Briguem 👀",
    "Peito ou bunda? Só pode escolher um 😏",
    "Qual a coisa mais safada que vocês já fizeram e nunca contaram pra ninguém? Prometo não julgar rs",
    "Duvido um homem aguentar 10 minutos comigo sem gozar 😏",
    "Homem de verdade aguenta a segunda logo depois da primeira? Ou é lenda? 👀",
    "Confesso: eu gosto mais de mandar nude do que de receber. É errado?",
    "Se esse post bater 1000 curtidas eu posto aquela foto que vocês pedem todo dia 🔥",
    "Vocês preferem mulher que geme alto ou baixinho? Sinceramente",
    "Com luz acesa ou apagada? Responde rápido 👀",
    "Homens: tamanho importa ou é desculpa de quem não sabe usar? Briguem rs",
    "Qual a primeira coisa que vocês olham numa mulher? Mentir é proibido 👀",
    "Sexo de manhã ou de madrugada? Escolhe um e defende",
    "Quantos minutos vocês aguentam? Sejam sinceros, ninguém tá olhando 😏",
    "Homens, vocês preferem que a mulher tome a iniciativa ou gostam de mandar?",
    "Coisas que todo homem faz na cama e acha que a gente gosta. A primeira é…",
    "Estou curiosa: homem casado é o que mais curte foto de mulher no X? 👀",
]
EVERGREEN = [
    "Qual a primeira coisa que vocês reparam numa mulher? Sinceridade 👀",
    "Morena ou loira? Só pode escolher uma rs",
    "Homem que manda bom dia primeiro já sai na frente, concordam? 😏",
    "Se esse post bater 500 curtidas eu posto aquela foto que vocês pediram rs",
    "Confesso: eu fico curiosa pra saber o que vocês pensam quando veem minha foto 👀",
    "Duvido um homem me responder sem usar emoji rs",
    "Praia ou piscina? Escolhe e me leva 😏",
    "Estou curiosa: o que faz um homem parar de rolar o feed? 👀",
]


# Prefixos que o Google Trends põe nas buscas de jogo: "Classificações de A x B" → "A x B".
GOOGLE_PREFIX = re.compile(r"^(?:classifica[cç](?:oes|ões)|escala[cç](?:oes|ões)|estat[ií]sticas|onde assistir|"
                           r"palpite|palpites|resultado|placar)(?:\s+(?:de|do|da|dos|das))?\s+", re.IGNORECASE)


def clean_title(title: str) -> str:
    """"#Flamengo" → "Flamengo"; hashtag CamelCase vira palavras; tudo minúsculo ou tudo maiúsculo vira
    "Copa do Brasil Sub 20" (palavras pequenas ficam minúsculas)."""
    text = GOOGLE_PREFIX.sub("", (title or "").strip())
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
    """O time quando ele é o assunto: "santos", "santos x vasco", "flamengo campeao". "Andrey Santos" é jogador."""
    for key, value in TEAMS.items():
        if norm == key or norm.startswith(key + " ") or (" x " in f" {norm} " and re.search(
                r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", norm)):
            return value
    return None


POOLS_LEVE = {
    "data": DATES, "team": FOOTBALL_TEAM, "person": PERSON, "match": MATCH, "football": FOOTBALL, "fight": FIGHT,
    "sports": SPORTS, "games": GAMES, "cars": CARS, "hangout": HANGOUT, "politics": POLITICS, "tv": TV,
    "music": MUSIC, "generic": GENERIC,
}
POOLS_QUENTE = {
    "data": DATES_Q, "team": FOOTBALL_TEAM_Q, "person": PERSON_Q, "match": MATCH_Q, "football": FOOTBALL_Q,
    "fight": FIGHT_Q, "sports": SPORTS_Q, "games": GAMES_Q, "cars": CARS_Q, "hangout": HANGOUT_Q,
    "politics": POLITICS_Q, "tv": TV_Q, "music": MUSIC_Q, "generic": GENERIC_Q,
}
HEATS = ("leve", "quente")


def kind_of(topic: dict[str, Any], title: str, kind: str | None = None) -> str:
    """Que tipo de assunto é (decide o banco de frases)."""
    norm = topic.get("norm") or src.norm(title)
    category = topic.get("category") or ""
    if kind == "data":
        return "data"
    if team_of(norm):
        return "team"
    if topic.get("sources") == ["wiki"] and 2 <= len(norm.split()) <= 4:
        return "person"  # verbete da Wikipédia com nome de gente
    if category == "Futebol":
        return "match" if " x " in title.lower() else "football"
    simple = {"Lutas": "fight", "Esportes": "sports", "Games": "games", "Carros": "cars", "Resenha": "hangout",
              "Política": "politics"}
    if category in simple:
        return simple[category]
    if is_person(norm, category):
        return "person"
    return {"Famosos/TV": "tv", "Música": "music"}.get(category, "generic")


def diverse(lines: list[str], key: str, size: int) -> list[str]:
    """Sorteio estável por chave, um formato diferente por linha antes de repetir formato (sem cara de lista igual)."""
    from app.radar_hot import format_of

    pool = list(dict.fromkeys(lines))
    random.Random(key).shuffle(pool)
    out, used = [], set()
    for line in pool:
        fmt = format_of(line)
        if fmt not in used:
            used.add(fmt)
            out.append(line)
    out += [line for line in pool if line not in out]
    return out[:size]


def ideas(topic: dict[str, Any], kind: str | None = None, heat: str = "leve", seed: int = 0) -> list[str]:
    """3 a 5 frases prontas para o assunto. kind="data" para datas sazonais; heat leve|quente; seed>0 sorteia
    outra combinação ("Mais ideias"). Leve com seed 0 mantém a ordem fixa de antes. Bloqueado = lista vazia."""
    if topic.get("blocked"):
        return []
    title = clean_title(topic.get("title") or topic.get("name") or "")
    if not title:
        return []
    norm = topic.get("norm") or src.norm(title)
    heat = heat if heat in HEATS else "leve"
    which = kind_of(topic, title, kind)
    team = team_of(norm) if which == "team" else None
    hot = heat == "quente"
    lines = list((POOLS_QUENTE if hot else POOLS_LEVE)[which])
    if which == "politics" and len(title.split()) > 2:
        # "Se X ganhar" só faz sentido com nome curto (Lula, Tarcísio), não com frase de protesto.
        lines = [line for line in lines if "{topic} ganhar" not in line]
    if (hot or seed) and which == "team" and " x " in title.lower():
        lines += MATCH_Q if hot else MATCH  # jogo de um grande: frases do time e do confronto no sorteio
    side_a, _, side_b = title.partition(" x ") if " x " in title else title.partition(" X ")

    def fill(templates: list[str]) -> list[str]:
        out: list[str] = []
        for line in templates:
            text = line.format(topic=title, team=team[0] if team else title, fans=team[1] if team else "",
                               a=side_a.strip() or title, b=side_b.strip() or title)
            text = text[0].upper() + text[1:]
            if text not in out:
                out.append(text)
        return out

    out = fill(lines)
    if not (hot or seed):
        return out[:MAX_IDEAS]
    key = f"{norm}|{heat}|{seed}"
    if which in ("person", "data"):
        return diverse(out, key, MAX_IDEAS)  # pessoa real e data só ganham as frases delas
    # 4 do tipo do assunto + 1 genérica sorteada: mais variedade sem diluir o que é do assunto.
    chosen = diverse(out, key, MAX_IDEAS - 1)
    extra = [line for line in diverse(fill(GENERIC_Q if hot else GENERIC), key, 5) if line not in chosen]
    return chosen + extra[:MAX_IDEAS - len(chosen)]


def hot_questions(heat: str = "quente", seed: int = 0, size: int = MAX_IDEAS) -> list[str]:
    """Perguntas quentes sem assunto (servem a qualquer hora), sorteadas pela semente."""
    lines = EVERGREEN_Q if heat == "quente" else EVERGREEN
    return diverse(lines, f"evergreen|{heat}|{seed}", size)


# Exemplos do próprio dono: o tom que dá milhões de views no nicho.
OWNER_EXAMPLES_LEVE = [
    "Se o Lula ganhar nessa eleição eu mando fotinha",
    "Se o Flamengo ganhar eu vou fazer uma coisa muuito pra frente rs",
    "Fiquei sabendo que os flamenguistas são os mais safadinhos… é verdade? 👀",
]
OWNER_EXAMPLES_QUENTE = [
    "Os homens gostam de buceta peluda ou lisa? Tem alguém que diz que não faz diferença? Estou curiosa",
    "Fantasias sexuais que toda mulher quer experimentar pelo menos uma vez. A primeira é",
    "Por que os homens acham difícil continuar transando depois que gozam, eles são fracos?",
    "Homens, eu prometo um nude dos seios aqui se o Lula ganhar",
    "Vocês homens gozam rápido mesmo?",
]
FORMAT_GUIDE = (
    "Formatos que viralizam (use um diferente em cada post): promessa condicional (\"se X ganhar eu…\", \"se esse "
    "post bater N comentários eu…\"); pergunta polêmica direto aos homens que mexe com o ego; enquete de duas "
    "opções (\"isso ou aquilo?\"); desafio (\"duvido um homem aguentar…\"); lista com gancho (\"…A primeira é\"); "
    "confissão ou curiosidade (\"estou curiosa\", \"confesso que…\"); duplo sentido."
)


def rules(heat: str) -> str:
    level = (
        "Nível QUENTE: pode ser direto e usar as palavras do nicho (nude, gozar, transar, peluda, lisinha, sentar, "
        "safada), em tom de provocação e pergunta; não descreva ato sexual passo a passo."
        if heat == "quente" else
        "Nível LEVE: sugestivo, nunca explícito (sem descrever sexo nem partes do corpo)."
    )
    return (
        f"{level}\nRegras fixas: nunca sexualizar uma pessoa real com nome (o assunto é só a condição ou o gancho, "
        "o conteúdo é sobre quem posta ou sobre os homens em geral); nada envolvendo menor de idade, falta de "
        "consentimento ou parentes; nada de ódio ou ofensa. Nunca mencione link, bio, grupo, Telegram, canal, "
        "privado, Pix ou venda: o post vive só do engajamento (respostas, curtidas, comentários)."
    )


def grok_prompt(topic: dict[str, Any], heat: str = "leve", refs: list[str] | None = None) -> str:
    title = clean_title(topic.get("title") or topic.get("name") or "")
    owner = OWNER_EXAMPLES_QUENTE if heat == "quente" else OWNER_EXAMPLES_LEVE
    lines = [
        "Você escreve posts curtos de texto para contas de modelos adultas (+18) no X, em português do Brasil, com "
        "gírias de internet. O público é só de homens. O objetivo é viralizar (milhões de views): pegar carona num "
        "assunto em alta e gerar respostas, citações e curtidas.",
        f"Assunto em alta agora: {title} (categoria: {topic.get('category') or 'geral'}).",
        "Tom dos posts do dono que funcionam: " + " | ".join(f'"{e}"' for e in owner),
    ]
    if refs:
        lines.append("Textos do nicho que estão engajando agora (referência de tom, não copie): "
                     + " | ".join(f'"{r}"' for r in refs))
    lines += [FORMAT_GUIDE, rules(heat),
              "Escreva 8 posts diferentes, um por linha, sem numeração nem aspas, cada um com até 200 caracteres, "
              "todos ligados ao assunto."]
    return "\n".join(lines)


def hot_prompt(heat: str = "quente", refs: list[str] | None = None, source: dict[str, Any] | None = None,
               hooks: list[str] | None = None) -> str:
    """Pedido ao Grok para a área de textos quentes: recriar um texto da biblioteca ou escrever textos novos."""
    owner = OWNER_EXAMPLES_QUENTE if heat == "quente" else OWNER_EXAMPLES_LEVE
    lines = [
        "Você escreve posts curtos de texto para contas de modelos adultas (+18) no X, em português do Brasil, com "
        "gírias de internet. O público é só de homens. O objetivo é viralizar (milhões de views) com respostas e "
        "discussão nos comentários.",
        "Tom dos posts do dono que funcionam: " + " | ".join(f'"{e}"' for e in owner),
    ]
    if refs:
        lines.append("Textos do nicho que estão engajando agora (referência, não copie): " + " | ".join(f'"{r}"' for r in refs))
    if source:
        lines.append(f"Texto que viralizou: \"{source['text']}\" (formato: {source['fmt_label']}; por que funciona: "
                     f"{source['why']}). Recrie a ideia em 6 versões novas, no mesmo formato, com outras palavras, "
                     "outros ângulos e outros ganchos. Nunca repita a frase original.")
    else:
        lines.append(FORMAT_GUIDE)
        if hooks:
            lines.append("Se fizer sentido, pegue carona em um destes assuntos em alta no Brasil: " + ", ".join(hooks) + ".")
        lines.append("Escreva 8 posts novos, um por linha, sem numeração nem aspas, cada um num formato diferente.")
    lines += [rules(heat), "Cada post com até 200 caracteres."]
    return "\n".join(lines)


def parse_lines(text: str, limit: int = MAX_IDEAS) -> list[str]:
    """Linhas do Grok, limpas; nunca passa frase com link/venda ou conteúdo proibido, mesmo que ele escreva."""
    from app.radar_hot import has_cta, unsafe

    out = []
    for raw in (text or "").splitlines():
        line = re.sub(r"^\s*(?:\d+\s*[.)-]|[-•*])\s*", "", raw).strip().strip('"').strip()
        if line and line not in out and not has_cta(line) and not unsafe(line):
            out.append(line[:280])
    return out[:limit]


def ask_grok_lines(key: str, prompt: str) -> str:
    """Texto do Grok para o pedido (chat completions da xAI). Erro vira ValueError com mensagem legível."""
    from app.services import grok_text

    model = os.getenv("XAI_MODEL", "grok-4.6")
    try:
        with httpx.Client(timeout=60.0, follow_redirects=False) as client:
            response = client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "temperature": 1.0, "messages": [{"role": "user", "content": prompt}]},
            )
    except httpx.HTTPError as exc:
        raise ValueError("Sem resposta da xAI. Tente de novo.") from exc
    if response.status_code >= 400:
        raise ValueError("A xAI recusou o pedido. Confira a chave e o modelo em Configurações.")
    try:
        return grok_text(response.json())
    except ValueError as exc:
        raise ValueError("A xAI não devolveu texto legível.") from exc
