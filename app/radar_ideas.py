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
from datetime import datetime, timezone
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
# Sem assunto: perguntas quentes que servem a qualquer hora ("Perguntas quentes"). Variadas por formato:
# enquete, pergunta polêmica, desafio, promessa, lista, confissão e duplo sentido.
EVERGREEN_Q = [
    # enquete
    "Os homens gostam de peluda ou lisinha? Tem alguém que diz que não faz diferença? Estou curiosa",
    "Peito ou bunda? Só pode escolher um 😏",
    "Com luz acesa ou apagada? Responde rápido 👀",
    "Sexo de manhã ou de madrugada? Escolhe um e defende",
    "Morena ou loira? E não vale dizer as duas rs",
    "Lingerie preta ou vermelha? Vou usar a que ganhar 🔥",
    "Rapidinha ou a noite toda? Só pode um 😏",
    "Vocês preferem mulher que geme alto ou baixinho? Sinceramente",
    "Beijo no pescoço ou puxão de cabelo? Escolhe 👀",
    "Por cima ou por baixo? Responde sem pensar rs",
    "Mulher tímida ou mulher safada? Os dois têm fã aqui, quero ver quem ganha",
    "Banho junto ou cada um no seu? rs",
    "Áudio gemendo ou foto? Qual deixa vocês mais loucos? 👀",
    "Primeiro encontro: só beijo ou já vai pra casa dela? Sinceramente",
    "Mulher que manda mensagem primeiro: atirada ou corajosa?",
    "Calcinha de renda ou sem calcinha? Pergunta séria 😏",
    "Mulher de salto ou de tênis? Defende a sua rs",
    "Cama, sofá ou chuveiro? Ordem de preferência, valendo 🔥",
    "Mulher baixinha ou alta? Só pode uma 👀",
    "Vocês preferem que a mulher fale safadeza ou fique quietinha?",
    # pergunta polêmica
    "Por que os homens acham difícil continuar transando depois que gozam, vocês são fracos?",
    "Vocês homens gozam rápido mesmo ou é só falta de prática? Sinceramente",
    "Por que todo homem acha que é bom de cama?",
    "Por que homem some depois que consegue o que quer?",
    "Vocês homens fingem que gostam de preliminar? Sejam sinceros",
    "Por que homem fica com sono logo depois? É preguiça ou cansaço mesmo? rs",
    "Homem de 30 é melhor de cama que de 20? Me convençam",
    "Qual a primeira coisa que vocês olham numa mulher? Mentir é proibido 👀",
    "Quantos minutos vocês aguentam? Sejam sinceros, ninguém tá olhando 😏",
    "Homens, vocês preferem que a mulher tome a iniciativa ou gostam de mandar?",
    "Vocês homens pensam em sexo quantas vezes por dia? Chuta um número",
    "Por que homem gosta tanto de foto de pé? Alguém me explica 👀",
    "O que vocês fariam se acordassem do meu lado? Quero ver criatividade 😏",
    "Qual parte do corpo da mulher vocês acham mais subestimada?",
    "Homem que manda nude sem pedir acha mesmo que tá agradando?",
    "Homens: tamanho importa ou é desculpa de quem não sabe usar? Briguem rs",
    "Vocês preferem mulher experiente ou que vocês ensinam?",
    "Por que homem acha que mulher não gosta de sexo tanto quanto ele?",
    "Homem que não beija bem merece segunda chance? Sinceramente",
    "Qual a coisa mais safada que vocês já fizeram e nunca contaram pra ninguém? Prometo não julgar rs",
    "Vocês já foram pegos no flagra? Conta como foi 👀",
    "Homem ciumento é chato ou é sinal de que gosta? Opinem",
    "Por que homem casado é o que mais chama mulher pra conversar? Alguém explica",
    "Qual foi o lugar mais louco que vocês já transaram?",
    "Homem que fala que nunca broxou tá mentindo? rs",
    # desafio
    "Duvido um homem aguentar 10 minutos comigo sem gozar 😏",
    "Homem de verdade aguenta a segunda logo depois da primeira? Ou é lenda? 👀",
    "Duvido você ler esse post até o fim sem pensar besteira rs",
    "Aposto que nenhum homem aqui aguenta uma noite inteira comigo",
    "Duvido um homem me descrever em 3 palavras sem ser safado",
    "Ninguém aguenta uma mulher que quer mais de uma vez, concordam?",
    "Duvido alguém comentar sem olhar minha foto primeiro rs",
    "Aposto que você abriu esse post esperando nude 👀",
    "Duvido um homem admitir nos comentários que já fingiu que gostou de algo",
    "Aposto que metade dos homens daqui não sabe onde fica o ponto G rs",
    "Duvido você mandar esse post pro seu amigo mais safado",
    # promessa
    "Se esse post bater 1000 curtidas eu posto aquela foto que vocês pedem todo dia 🔥",
    "Se esse post bater 500 comentários eu posto sem censura 😏",
    "Se esse post bater 300 respostas eu conto minha fantasia mais safada",
    "Prometo responder todo mundo que comentar o que faria comigo rs",
    "Se eu chegar em 1000 curtidas hoje eu posto de lingerie 🔥",
    "Se esse post bater 200 RT eu mando nude no próximo post rs",
    "Se esse post passar de 100 comentários eu conto o que eu tô vestindo agora 👀",
    "Prometo que quem comentar primeiro vai ganhar minha resposta mais safada rs",
    # lista
    "Fantasias que toda mulher quer experimentar pelo menos uma vez. A primeira é…",
    "Coisas que todo homem faz na cama e acha que a gente gosta. A primeira é…",
    "Sinais de que um homem é bom de cama. O primeiro é…",
    "Coisas que deixam qualquer mulher louca em 5 segundos. A primeira é…",
    "Motivos que fazem uma mulher nunca mais responder um homem. O primeiro é…",
    "Tipos de homem que toda mulher já pegou. O primeiro é…",
    "Coisas que eu faria se tivesse coragem. A primeira é…",
    "Erros que todo homem comete no primeiro encontro. O primeiro é…",
    # confissão
    "Confesso: eu gosto mais de mandar nude do que de receber. É errado?",
    "Confissão: eu fico molhadinha só de ler os comentários de vocês rs",
    "Confesso que já mandei nude pra pessoa errada… quem nunca?",
    "Admito: homem de moletom cinza me desconcentra 👀",
    "Confesso que durmo sem calcinha. Vocês dormem como? rs",
    "Admito que já fingi… e vocês, já perceberam alguma vez?",
    "Estou curiosa: homem gosta mais de mulher que manda áudio ou foto?",
    "Confesso que leio todos os comentários safados de vocês antes de dormir rs",
    "Admito: voz grossa me ganha antes de qualquer foto 👀",
    "Estou curiosa: quantos aqui já se apaixonaram por alguém do X?",
    # duplo sentido
    "Hoje eu acordei querendo sentar… na cadeira do trabalho, claro rs",
    "Tô precisando de alguém pra me ajudar a abrir um pote… e outras coisas rs",
    "Procura-se homem que saiba usar as mãos. Pra montar um móvel, claro rs",
    "Hoje eu tô carente de atenção e de outras coisas 👀",
    "Não sei cozinhar, mas sei comer muito bem rs",
    "Ajoelhou tem que rezar, né? rs",
    "Gosto de homem que pega firme… no volante, claro 😏",
    "Tô com vontade de fazer uma coisa que começa com S e termina com O rs",
    "Mulher sozinha em casa num sábado… o que vocês acham que eu tô fazendo? 👀",
    "Meu ponto fraco é homem que sabe o que faz com a boca. Conversando, claro rs",
    "Hoje eu só queria alguém pra me fazer suar… na academia, óbvio 😏",
    "Tô precisando de uma mão amiga. Ou duas rs",
]
# Por hora do dia em São Paulo (moment_of): bom dia, madrugada e fim de semana.
MORNING_Q = [
    "Bom dia pra quem acordou animado hoje… vocês me entenderam rs",
    "Bom dia! Vocês acordam com vontade ou é só eu? 👀",
    "Café ou sexo de manhã? Só pode um rs",
    "Acordei sem roupa e sem coragem de levantar. E vocês? 😏",
    "Bom dia só pra quem pensou besteira antes de levantar rs",
    "Quem aí já acordou pensando em mim? Sinceramente 👀",
]
NIGHT_Q = [
    "Quem tá acordado essa hora tá pensando em quê? Sinceramente 👀",
    "Madrugada e eu sem sono… alguém me cansa? rs",
    "Insônia ou saudade de alguém na cama? 😏",
    "Quem tá online agora comenta, vamos ver quem tá sozinho rs",
    "Deitada aqui só de camiseta… alguém mais acordado? 👀",
    "Duvido quem tá acordado agora não estar pensando besteira rs",
]
WEEKEND_Q = [
    "Sextou! Vocês vão sair ou ficar em casa fazendo besteira? rs",
    "Final de semana bom é o que termina com a cama bagunçada, concordam? 😏",
    "Sábado à noite: balada ou motel? Sinceramente rs",
    "Domingo é dia de ficar na cama… sozinha não tem graça 👀",
    "Fim de semana chegou e eu tô sem ninguém pra estragar minha reputação rs",
    "Se esse post bater 500 curtidas até domingo eu posto aquela foto 🔥",
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
    "Vestido ou calça jeans? Qual fica melhor numa mulher? rs",
    "Sinais de que um homem tá interessado. O primeiro é…",
    "Homem cheiroso já ganha metade da conversa, concordam?",
    "Primeiro encontro: cinema ou barzinho? Escolhe 👀",
]
MORNING = ["Bom dia! Quem aí já tomou café pensando em mim? rs", "Bom dia pra quem me deu bom dia primeiro 👀"]
NIGHT = ["Quem tá acordado essa hora? Me conta o que tá fazendo rs", "Boa noite pra quem ainda tá aqui comigo 👀"]
WEEKEND = ["Sextou! Qual o plano de vocês pra hoje? rs", "Domingo preguiçoso: cama ou rolê? 👀"]


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


def ideas(topic: dict[str, Any], kind: str | None = None, heat: str = "leve", seed: int = 0,
          proven: list[dict[str, Any]] | None = None) -> list[str]:
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
    # 4 do tipo do assunto + 1 inspirada no que já engajou (ou genérica sorteada, sem comprovados).
    chosen = diverse(out, key, MAX_IDEAS - 1)
    if hot and proven:
        inspired = adapt_proven(title, proven, key)
        if inspired and inspired not in chosen:
            return chosen + [inspired]
    extra = [line for line in diverse(fill(GENERIC_Q if hot else GENERIC), key, 5) if line not in chosen]
    return chosen + extra[:MAX_IDEAS - len(chosen)]


# ---- Variações grátis de um texto que engajou: mantém a ideia, troca abertura, fecho, meta e ordem ----
MEN_OPENERS = ["Homens,", "Vocês homens,", "Sinceramente, homens:", "Pergunta séria pros homens:", "Me respondam, homens:"]
SOFT_OPENERS = ["Pergunta séria:", "Sinceramente:", "Me digam uma coisa:", "Confissão rápida:", "Preciso saber:"]
CLOSERS = ["rs", "👀", "😏", "🔥", "Sinceramente", "Estou curiosa", "Quero ver quem responde com sinceridade"]
MEN_START = re.compile(r"^(?:homens|voc[eê]s homens|os homens)\b[,:]?\s*", re.IGNORECASE)
SOFT_START = re.compile(r"^(?:pergunta s[eé]ria|sinceramente|me digam uma coisa|confiss[aã]o r[aá]pida|preciso saber)\s*[:,]\s*", re.IGNORECASE)
TAIL = re.compile(r"[\s,]*(?:\b(?:rs+|kk+|sinceramente|estou curiosa)\b|[👀😏🔥🤭😈🙈🫣]|\s)*[.!…]*$", re.IGNORECASE)
GOAL = re.compile(r"\b(\d{2,5})\s+(curtidas|coment[aá]rios|respostas|rts?|reposts|likes)\b", re.IGNORECASE)
POLL_START = re.compile(r"^([^?,:]{2,30}?) ou ([^?,:]{2,30}?)\?(.*)$")
GOAL_UNITS = ["comentários", "respostas", "curtidas"]
GOAL_NUMBERS = (100, 200, 300, 500, 1000)  # tupla: as listas do módulo são só de frases


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _low(text: str) -> str:
    return text[:1].lower() + text[1:] if text and not text[:2].isupper() else text


def variations(text: str, n: int = 6, seed: int = 0) -> list[str]:
    """Versões novas de um texto que já engajou, sem API: o miolo fica e mudam a abertura, o fecho, a meta da
    promessa e a ordem da enquete. Regras para soar natural:
    - "Homens," só entra se o texto não fala dos homens em 3ª pessoa (senão vira "Homens, os homens…"); se o
      texto já abria chamando os homens, a chamada é trocada por outra;
    - aberturas e fechos com palavras ("Sinceramente", "Estou curiosa") só em pergunta; afirmação e promessa
      ganham só emoji no fim.
    Nunca devolve o próprio texto; nada com link, venda ou conteúdo proibido."""
    from app.radar_hot import has_cta, unsafe

    original = " ".join((text or "").split())
    if not original or has_cta(original) or unsafe(original):
        return []
    rng = random.Random(f"var|{src.norm(original)}|{seed}")
    core = TAIL.sub("", original).strip() or original
    question = "?" in core
    address = re.match(r"^(?:homens|voc[eê]s homens)\s*[,:]\s*", core, re.IGNORECASE)  # "Homens, …" chamando
    soft = SOFT_START.match(core)
    body = core[(address or soft).end():].strip() if (address or soft) else core
    talks_about_men = bool(re.search(r"\bhomens?\b", body, re.IGNORECASE))
    candidates: list[tuple[str, str]] = []

    def add(kind: str, line: str) -> None:
        line = _cap(" ".join(line.split()))
        # Texto exato (o norm ignora emoji e acharia "texto 👀" igual ao original).
        if line and line.casefold() != original.casefold() and len(line) <= 280 and all(line != c for _, c in candidates):
            candidates.append((kind, line))

    openers = []
    if address or not talks_about_men:
        openers += MEN_OPENERS if question else MEN_OPENERS[:2]  # "Me respondam, homens:" só em pergunta
    if question:
        openers += SOFT_OPENERS
    for opener in rng.sample(openers, len(openers)):
        add("abre", f"{opener} {_low(body)}")
    goal = GOAL.search(body)
    if goal:
        number = int(goal.group(1))
        for target in rng.sample([g for g in GOAL_NUMBERS if g != number], 3):
            add("meta", GOAL.sub(f"{target} {rng.choice(GOAL_UNITS)}", core, count=1))
    poll = POLL_START.match(body)
    if poll and len(poll.group(1).split()) <= 3 and len(poll.group(2).split()) <= 3:
        add("enquete", f"{_cap(poll.group(2).strip())} ou {_low(poll.group(1).strip())}?{poll.group(3)}")
        add("enquete", f"Só pode escolher um: {_low(poll.group(1).strip())} ou {_low(poll.group(2).strip())}?")
    closers = [c for c in CLOSERS if core.endswith("?") or not c[0].isalpha()]  # palavra só depois de "?"
    for closer in rng.sample(closers, len(closers)):
        add("fecho", f"{core} {closer}")
    rng.shuffle(candidates)
    picked: list[str] = []
    for kind in ("abre", "meta", "enquete", "fecho"):  # uma de cada tipo de mudança primeiro
        hit = next((line for k, line in candidates if k == kind and line not in picked), None)
        if hit:
            picked.append(hit)
    picked += [line for _, line in candidates if line not in picked]
    return [line for line in picked if not has_cta(line) and not unsafe(line)][:n]


# Gancho para levar um texto comprovado (sem assunto) para um assunto em alta.
PROVEN_HOOKS = ["Todo mundo falando de {topic} e eu aqui com uma dúvida: ", "{topic} em alta e eu só penso nisso: ",
                "Esquece {topic} um minuto: "]
PROVEN_FORMATS = ("pergunta", "enquete", "confissao", "desafio")


def adapt_proven(topic_title: str, proven: list[dict[str, Any]], key: str) -> str | None:
    """Um texto que já engajou, levado para o assunto em alta por um gancho (só perguntas, enquetes,
    confissões e desafios: promessa e lista dependem do próprio assunto)."""
    pool = [p for p in proven if p.get("fmt") in PROVEN_FORMATS and len(p["text"]) <= 200]
    if not pool:
        return None
    rng = random.Random(f"proven|{key}")
    item = rng.choice(pool)
    text = item["text"] if item.get("source") == "sua" else (variations(item["text"], 1, seed=rng.randint(0, 999)) or [None])[0]
    if not text:
        return None
    line = rng.choice(PROVEN_HOOKS).format(topic=topic_title) + _low(text)
    return line if len(line) <= 280 else None


# ---- Dia da semana: "sextou" só na sexta, "domingou" só no domingo ----
WEEKDAY_NAMES = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
# (regex no texto normalizado, dias em que pode sair; 0 = segunda). Tuplas: as listas do módulo são só frases. "Segunda chance" não é dia: só "segunda feira".
DAY_WORDS = (
    (re.compile(r"\bate (?:o )?doming\w*\b"), {4, 5, 6}),
    (re.compile(r"\b(?:fim|final) de semana\b|\bfds\b|\bfinde\b"), {4, 5, 6}),
    (re.compile(r"\bsext(?:a|ou+|ouu+)\b|\bsexta feira\b"), {4}),
    (re.compile(r"\bsabad(?:o|ou+)\b"), {5}),
    (re.compile(r"\bdoming(?:o|ou+)\b"), {6}),
    (re.compile(r"\bsegunda feira\b|\bsegundou+\b"), {0}),
    (re.compile(r"\bterca feira\b|\btercou+\b"), {1}),
    (re.compile(r"\bquarta feira\b|\bquartou+\b"), {2}),
    (re.compile(r"\bquinta feira\b|\bquintou+\b"), {3}),
)


def now_sp() -> datetime:
    from app.radar import DEFAULT_TZ_INFO

    return datetime.now(timezone.utc).astimezone(DEFAULT_TZ_INFO)


def fits_day(text: str, now: datetime | None = None) -> bool:
    """Frase que cita um dia da semana só sai nesse dia (hora de São Paulo). "Até domingo" e "fim de semana"
    valem de sexta a domingo."""
    from app.radar import DEFAULT_TZ_INFO

    day = (now or now_sp()).astimezone(DEFAULT_TZ_INFO).weekday()
    key = src.norm(text)
    for pattern, days in DAY_WORDS:
        if pattern.search(key):
            if day not in days:
                return False
            key = pattern.sub(" ", key)  # "até domingo" não cai de novo na regra do domingo
    return True


def today_line(now: datetime | None = None) -> str:
    day = (now or now_sp()).weekday()
    return (f"Hoje é {WEEKDAY_NAMES[day]}: se citar dia da semana, só o de hoje (nada de \"sextou\" fora da sexta, "
            "\"domingou\" fora do domingo).")


# ---- Gerador quente: monta frases novas combinando peças (grátis, sem API) ----
# Persona: mulher adulta provocando o público masculino. Pesado e direto no vocabulário, sempre em tom de
# pergunta, enquete, desafio, promessa, lista, confissão ou duplo sentido; nunca menor, sem consentimento ou
# parente (o filtro unsafe confere tudo no fim), nunca link/venda.
MIX_SIZE = 60
POLL_PAIRS = (
    ("de quatro", "por cima"), ("peito", "bunda"), ("chupar", "ser chupado"), ("puxão de cabelo", "tapa na bunda"),
    ("gemido alto", "sussurro no ouvido"), ("lisinha", "peludinha"), ("na cama", "no chuveiro"),
    ("sexo selvagem", "bem devagarinho"), ("lingerie", "pelada direto"), ("rapidinha no banheiro", "a noite inteira no motel"),
    ("sentada lenta", "sentada forte"), ("gozar junto", "ela primeiro"), ("nude de frente", "nude de costas"),
    ("acordar com boquete", "acordar com café"), ("transar de manhã", "transar de madrugada"),
    ("mulher que geme alto", "mulher que morde o travesseiro"), ("beijo no pescoço", "mordida na orelha"),
    ("mulher safada", "mulher tímida que vira bicho na cama"), ("luz acesa", "luz apagada"), ("sofá", "mesa da cozinha"),
    ("calcinha de renda", "sem calcinha"), ("ela pedindo mais", "ela pedindo pra parar de tanto gozar"),
    ("dedada", "língua"), ("espelho", "câmera ligada"), ("no carro", "no motel"), ("cavalgada", "papai e mamãe bem feito"),
    ("conversa safada antes", "ir direto ao ponto"), ("mulher que manda", "mulher que obedece"),
)
POLL_FRAMES = [
    "{A} ou {b}? {close}", "Pergunta séria pros homens: {a} ou {b}?", "Só pode escolher um: {a} ou {b}? {emoji}",
    "{A} ou {b}? Responde sem pensar e sem mentir rs", "Homens, {a} ou {b}? Quero ver quem tem coragem de responder",
    "Vou fazer o que ganhar nos comentários: {a} ou {b}? 😈", "{A} ou {b}? Defende a sua nos comentários {emoji}",
]
QUESTIONS = [
    "vocês aguentam quantos minutos sem gozar", "por que homem goza e vira pro lado pra dormir",
    "vocês sabem mesmo achar o clitóris ou é sorte", "vocês preferem chupar ou ser chupados",
    "por que todo homem jura que é bom de cama e quase nenhum é", "vocês já broxaram na hora H",
    "o que vocês fariam se eu sentasse no colo de vocês agora", "quantas vezes por semana é o mínimo pra vocês",
    "vocês gostam de mulher que fala putaria na cama", "por que homem tem medo de mulher que sabe o que quer na cama",
    "qual a posição que vocês nunca tiveram coragem de pedir", "vocês dão conta de uma mulher que quer três vezes seguidas",
    "qual parte do meu corpo vocês beijariam primeiro", "vocês gozam rápido mesmo ou é falta de prática",
    "tamanho importa ou é desculpa de quem não sabe usar", "vocês já fingiram que gozaram",
    "o que deixa vocês duros mais rápido, foto ou áudio", "qual a coisa mais safada que uma mulher já fez com vocês",
    "vocês preferem mulher que pede ou mulher que manda", "por que homem ama tanto uma mulher de quatro",
    "vocês aguentam a segunda logo depois da primeira", "qual foi o lugar mais arriscado que vocês já transaram",
    "vocês deixariam eu mandar em vocês por uma noite inteira", "o que vocês pensam quando veem uma mulher de legging",
]
Q_OPENERS = ["Homens,", "Vocês homens,", "Sinceramente, homens:", "Pergunta séria:", "Me respondam uma coisa:", "Preciso saber:", ""]
Q_CLOSERS = ["Sinceramente", "Estou curiosa", "rs", "😏", "👀", "Sem mentir", "Ninguém tá olhando rs", "Quero ver quem é sincero"]
DARES = [
    "Duvido um homem aguentar {n} minutos comigo sem gozar", "Duvido você ler isso até o fim sem ficar duro",
    "Aposto que nenhum homem aqui aguenta eu sentando sem parar", "Duvido um homem me deixar sem fôlego",
    "Duvido alguém aqui me fazer gozar duas vezes na mesma noite", "Aposto que você não saberia o que fazer comigo molhadinha",
    "Duvido você comentar o que faria comigo sem ficar com vergonha", "Aposto que metade dos homens daqui nunca fez uma mulher tremer",
    "Duvido um homem me descrever em 3 palavras sem ser safado", "Aposto que você já imaginou eu de quatro e nem disfarça",
    "Duvido você aguentar uma noite inteira no meu ritmo", "Aposto que você abriu esse post achando que ia ter nude",
]
DARE_CLOSERS = ["😏", "rs", "Prova nos comentários", "Quero ver quem é homem de verdade aqui 😈", "🔥", "Comenta se tiver coragem"]
GOALS = ((100, "comentários"), (200, "comentários"), (300, "respostas"), (500, "curtidas"), (1000, "curtidas"), (200, "RT"))
REWARDS = [
    "posto um nude sem censura", "posto aquela foto de quatro", "conto com detalhes a minha última transa",
    "posto uma foto só de calcinha", "gravo um áudio gemendo", "conto a minha fantasia mais suja",
    "posto a foto do espelho sem nada", "respondo todo mundo do jeito mais safado possível",
    "conto o lugar mais louco onde eu já transei", "posto a foto que eu nunca tive coragem",
]
PROMISE_FRAMES = [
    "Se esse post bater {n} {unit} eu {reward} {emoji}", "{n} {unit} e eu {reward}. Combinado? 😈",
    "Bateu {n} {unit}, eu {reward}. Valendo {emoji}", "Prometo: se chegar em {n} {unit} eu {reward} {emoji}",
]
LISTS = [
    "Coisas que eu faria com você numa noite sem limite. A primeira é…", "Lugares onde eu já transei e ninguém sabe. O primeiro é…",
    "Posições que toda mulher ama e homem nunca oferece. A primeira é…",
    "Coisas que um homem faz na cama que me deixam molhada na hora. A primeira é…",
    "Fantasias que eu nunca tive coragem de realizar. A primeira é…", "Sinais de que ele vai ser bom de cama. O primeiro é…",
    "Erros que fazem uma mulher fingir o orgasmo. O primeiro é…", "Coisas que eu amo ouvir no meio da transa. A primeira é…",
    "Motivos pra nunca mais transar com um homem. O primeiro é…", "Coisas que toda mulher faz sozinha e não conta. A primeira é…",
]
CONFESSIONS = [
    "fico molhadinha lendo os comentários de vocês", "já transei no carro no estacionamento do shopping",
    "durmo sem calcinha todo dia", "já me toquei pensando num seguidor daqui", "amo quando puxam meu cabelo na hora",
    "já fingi orgasmo pra acabar logo", "mandei nude pro contatinho errado", "gemo tão alto que o vizinho já reclamou",
    "o que mais me excita é homem que manda em mim na cama", "já gozei só com beijo no pescoço",
    "tenho uma pasta de fotos que ninguém nunca viu", "fico com vontade toda vez que leio um comentário safado",
]
CONF_FRAMES = ["Confesso: eu {c}. {follow}", "Confissão de hoje: eu {c} 🙈 {follow}", "Admito que eu {c}. {follow}",
               "Ninguém sabe disso, mas eu {c} 🤭 {follow}", "Estou curiosa: vocês acreditam que eu {c}?"]
CONF_FOLLOW = ["Isso é errado?", "Quem mais?", "Vocês me julgam?", "E vocês, já?", "Me conta o seu agora", ""]
DOUBLE = [
    "Hoje tô com uma vontade de sentar que nem cadeira resolve rs", "Procuro homem que saiba usar a língua… pra conversar, claro 😏",
    "Quero alguém pra me dar uma mão… e o resto também rs", "Tô tão carente que até o chuveiro tá recebendo atenção 👀",
    "Minha especialidade é engolir… sapo, na vida. Claro rs", "Tô precisando de alguém que me pegue de jeito… no abraço, óbvio 😈",
    "Sou ótima em montar… móvel, gente. Que mente suja rs", "Hoje eu só queria alguém pra me deixar sem voz… cantando, claro 😏",
]
EMOJI = ["🔥", "😏", "😈", "👀"]


def _poll(rng: random.Random) -> str:
    a, b = rng.choice(POLL_PAIRS)
    if rng.random() < .5:
        a, b = b, a
    return rng.choice(POLL_FRAMES).format(A=_cap(a), a=a, b=b, close=rng.choice(Q_CLOSERS), emoji=rng.choice(EMOJI))


def _question(rng: random.Random) -> str:
    core = rng.choice(QUESTIONS)
    openers = [o for o in Q_OPENERS if "homens" not in o.lower()] if re.search(r"\bhom(?:em|ens)\b", core) else Q_OPENERS  # sem "Homens, …todo homem"
    opener = rng.choice(openers)
    line = (f"{opener} {core}?" if opener else _cap(core) + "?")
    return f"{line} {rng.choice(Q_CLOSERS)}"


def _dare(rng: random.Random) -> str:
    closer = rng.choice(DARE_CLOSERS)
    joint = ". " if closer[0].isalpha() else " "  # "…sem gozar. Prova nos comentários" / "…sem gozar 😏"
    return rng.choice(DARES).format(n=rng.choice((5, 10, 15))) + joint + closer


def _promise(rng: random.Random) -> str:
    n, unit = rng.choice(GOALS)
    return rng.choice(PROMISE_FRAMES).format(n=n, unit=unit, reward=rng.choice(REWARDS), emoji=rng.choice(EMOJI))


def _confession(rng: random.Random) -> str:
    return rng.choice(CONF_FRAMES).format(c=rng.choice(CONFESSIONS), follow=rng.choice(CONF_FOLLOW))


MAKERS = (_poll, _question, _dare, _promise, _confession, _poll, _question,
          lambda rng: rng.choice(LISTS), lambda rng: rng.choice(DOUBLE))


def compose(heat: str = "quente", seed: int = 0, size: int = MIX_SIZE, now: datetime | None = None) -> list[str]:
    """Frases quentes novas montadas por peças (abertura, miolo, fecho, meta, prêmio). A semente muda a mistura;
    só o nível quente (o leve fica no banco fixo, sugestivo)."""
    from app.radar_hot import has_cta, unsafe

    if heat != "quente":
        return []
    rng = random.Random(f"mix|{seed}")
    out: list[str] = []
    seen: set[str] = set()
    for _ in range(size * 6):
        if len(out) >= size:
            break
        line = " ".join(rng.choice(MAKERS)(rng).split())
        key = src.norm(line)
        if key in seen or len(line) > 220 or unsafe(line) or has_cta(line) or not fits_day(line, now):
            continue
        seen.add(key)
        out.append(line)
    return out


def hot_questions(heat: str = "quente", seed: int = 0, size: int = MAX_IDEAS, now: datetime | None = None) -> list[str]:
    """Perguntas quentes sem assunto (servem a qualquer hora), sorteadas pela semente: banco fixo + frases
    montadas, só as que cabem no dia de hoje."""
    lines = (EVERGREEN_Q + compose(heat, seed, now=now)) if heat == "quente" else EVERGREEN
    return diverse([line for line in lines if fits_day(line, now)], f"evergreen|{heat}|{seed}", size)


# Momento do dia (São Paulo): (nome na tela, banco quente, banco leve).
MOMENTS = {
    "manha": ("Bom dia", MORNING_Q, MORNING),
    "madrugada": ("Madrugada", NIGHT_Q, NIGHT),
    "fds": ("Fim de semana", WEEKEND_Q, WEEKEND),
}


def moment_of(now: datetime | None = None) -> str | None:
    """manha (5h–11h), madrugada (23h–5h), fds (sexta depois das 17h, sábado e domingo) ou None."""
    from app.radar import DEFAULT_TZ_INFO

    local = (now or datetime.now(timezone.utc)).astimezone(DEFAULT_TZ_INFO)
    if local.hour >= 23 or local.hour < 5:
        return "madrugada"
    if local.hour < 11:
        return "manha"
    if local.weekday() in (5, 6) or (local.weekday() == 4 and local.hour >= 17):
        return "fds"
    return None


def question_bank(heat: str = "quente", now: datetime | None = None,
                  proven: list[dict[str, Any]] | None = None, mix: int = 0) -> dict[str, Any]:
    """O banco inteiro de perguntas sem assunto, com formato e momento; a tela sorteia, filtra e marca as usadas.
    Com proven: as que já bombaram nas suas contas (iguais, para repostar depois) e variações das do nicho
    (nunca o texto de outra conta igual), com source "sua" ou "nicho". mix: semente das frases montadas pelo
    gerador (made=True). Frase que cita outro dia da semana fica de fora."""
    from app.radar_hot import LABELS, format_of

    hot = heat == "quente"
    moment = moment_of(now)
    items = []
    for item in proven or []:
        if item["source"] == "sua":
            items.append({"text": item["text"], "moment": None, "source": "sua", "views": item["views"], "replies": item["replies"]})
        else:
            for line in variations(item["text"], 2):
                items.append({"text": line, "moment": None, "source": "nicho", "views": item["views"], "replies": item["replies"]})
    for key, (_, quente, leve) in MOMENTS.items():
        items += [{"text": text, "moment": key} for text in (quente if hot else leve)]
    items += [{"text": text, "moment": None} for text in (EVERGREEN_Q if hot else EVERGREEN)]
    items += [{"text": text, "moment": None, "made": True} for text in compose(heat, mix, now=now)]
    items = [item for item in items if fits_day(item["text"], now)]
    for item in items:
        item["fmt"] = format_of(item["text"])
        item["fmt_label"] = LABELS[item["fmt"]][0]
    seen, unique = set(), []
    for item in items:
        if src.norm(item["text"]) not in seen:
            seen.add(src.norm(item["text"]))
            unique.append(item)
    items = unique
    return {"heat": heat, "mix": mix, "moment": moment, "moment_label": MOMENTS[moment][0] if moment else None,
            "moments": {key: label for key, (label, _, _) in MOMENTS.items()}, "items": items}


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
        "Nível QUENTE: pesado, provocante e sem pudor no vocabulário: use os termos sexuais do nicho (nude, gozar, "
        "transar, chupar, boquete, sentar, de quatro, cavalgar, gemer, molhadinha, duro, peluda, lisinha, safada) "
        "sempre em tom de provocação, pergunta, desafio, promessa ou confissão de quem posta; o texto instiga, não "
        "narra cena de sexo passo a passo."
        if heat == "quente" else
        "Nível LEVE: sugestivo, nunca explícito (sem descrever sexo nem partes do corpo)."
    )
    return (
        f"{level}\n{today_line()}\nRegras fixas: nunca sexualizar uma pessoa real com nome (o assunto é só a condição ou o gancho, "
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
        lines.append("Textos que já engajaram de verdade (das contas do dono e do nicho, com os números; use como "
                     "referência do que funciona, não copie): " + " | ".join(r if r.startswith('"') else f'"{r}"' for r in refs))
    lines += [FORMAT_GUIDE, rules(heat),
              "Escreva 8 posts diferentes, um por linha, sem numeração nem aspas, cada um com até 200 caracteres, "
              "todos ligados ao assunto."]
    return "\n".join(lines)


HOT_COUNT = 10  # textos novos por pedido ao Grok


def bank_examples(fmt: str | None, heat: str = "quente", size: int = 6, seed: str = "") -> list[str]:
    """Frases do banco no formato pedido (ou variadas): referência de tom para o Grok."""
    from app.radar_hot import format_of

    pool = EVERGREEN_Q if heat == "quente" else EVERGREEN
    if fmt:
        pool = [line for line in pool if format_of(line) == fmt] or pool
    return diverse(pool, f"bank|{fmt}|{heat}|{seed}", size)


def hot_prompt(heat: str = "quente", refs: list[str] | None = None, source: dict[str, Any] | None = None,
               hooks: list[str] | None = None, fmt: str | None = None, seed: str = "") -> str:
    """Pedido ao Grok para a área de textos quentes: recriar um texto (da biblioteca ou do banco) ou escrever
    textos novos, todos num formato (fmt) ou um de cada."""
    from app.radar_hot import LABELS

    owner = OWNER_EXAMPLES_QUENTE if heat == "quente" else OWNER_EXAMPLES_LEVE
    lines = [
        "Você é a melhor copywriter de contas de modelos adultas (+18) de texto no X, em português do Brasil, com "
        "gírias de internet. O público é só de homens. O objetivo é viralizar (milhões de views): o algoritmo do X "
        "entrega o post que gera respostas e discussão, então cada texto precisa fazer o homem querer responder, "
        "discordar, escolher um lado ou provar alguma coisa nos comentários.",
        "Tom dos posts do dono que funcionam: " + " | ".join(f'"{e}"' for e in owner),
        "Mais exemplos no tom certo: " + " | ".join(f'"{e}"' for e in bank_examples(fmt, heat, 6, seed)),
    ]
    if refs:
        lines.append("Textos que já engajaram de verdade (das contas do dono e do nicho, com os números; use como "
                     "referência do que funciona, não copie): " + " | ".join(r if r.startswith('"') else f'"{r}"' for r in refs))
    lines.append(
        "O que faz viralizar: primeira linha curta que prende; pergunta direta aos homens (\"vocês homens…\", "
        "\"homens,…\"); mexer com o ego masculino (duração, tamanho, pegada, fôlego); escolha entre duas opções; "
        "curiosidade (\"estou curiosa\"); promessa que o público quer cobrar; terminar com pergunta, \"rs\" ou um "
        "emoji (😏 👀 🔥). Frases naturais, como uma mulher real escreveria no celular, sem cara de anúncio."
    )
    lines.append(
        "Seja criativa e variada: não repita a estrutura nem as palavras dos exemplos; invente ângulos novos "
        "(situações do dia a dia com duplo sentido, comparações, números, cenários, provocação ao ego, fantasias "
        "comuns entre adultos, a reação dela lendo os comentários). Cada texto com uma ideia diferente."
    )
    if source:
        lines.append(f"Texto que viralizou: \"{source['text']}\" (formato: {source['fmt_label']}; por que funciona: "
                     f"{source['why']}). Recrie a ideia em 6 versões novas, no mesmo formato, com outras palavras, "
                     "outros ângulos e outros ganchos. Nunca repita a frase original.")
    else:
        if fmt and fmt in LABELS:
            label, why = LABELS[fmt]
            lines.append(f"Todos no formato {label}: {why}")
        else:
            lines.append(FORMAT_GUIDE)
        if hooks:
            lines.append("Pode pegar carona em um destes assuntos em alta no Brasil quando encaixar naturalmente: "
                         + ", ".join(hooks) + ". A maioria dos textos deve funcionar sem assunto nenhum.")
        else:
            lines.append("Sem assunto do momento: textos que funcionam a qualquer hora, em qualquer dia.")
        lines.append(f"Escreva {HOT_COUNT} posts novos e diferentes entre si, um por linha, sem numeração nem aspas"
                     + (", cada um num formato diferente." if not fmt else "."))
    lines += [rules(heat), "Cada post com até 200 caracteres. Não explique nada, só os posts."]
    return "\n".join(lines)


def champions_prompt(heat: str, champions: list[str]) -> str:
    """Pedido ao Grok: variações dos textos que mais bombaram (2 por texto), no mesmo formato e com a mesma
    ideia, mas palavras e ganchos novos."""
    owner = OWNER_EXAMPLES_QUENTE if heat == "quente" else OWNER_EXAMPLES_LEVE
    return "\n".join([
        "Você é a melhor copywriter de contas de modelos adultas (+18) de texto no X, em português do Brasil, com "
        "gírias de internet. O público é só de homens. O objetivo é viralizar com respostas e discussão.",
        "Tom do dono: " + " | ".join(f'"{e}"' for e in owner),
        "Estes textos bombaram de verdade (com os números): " + " | ".join(champions),
        "Para cada um, escreva 2 variações: mesmo formato e mesma ideia que fez engajar, com outras palavras, "
        "outro gancho e outro fecho. Nunca repita a frase original. Um post por linha, sem numeração nem aspas.",
        rules(heat), "Cada post com até 200 caracteres. Não explique nada, só os posts.",
    ])


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
