"""Calendário de datas brasileiras que costumam virar assunto, para a faixa "Próximos dias" do Radar.

As datas móveis são calculadas por ano (Páscoa pelo algoritmo de Meeus/Jones/Butcher); eleições só nos anos pares
(1º turno no 1º domingo de outubro, 2º turno no último).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

# Dica de ângulo de post por data (curta, para caber no cartão).
TIPS = {
    "Ano Novo": "Metas e retrospectiva: enquete \"o que você quer em 2027?\".",
    "Carnaval": "Blocos, fantasias e memes de ressaca rendem muito.",
    "Quarta de Cinzas": "\"Voltando à vida real\": humor do pós-Carnaval.",
    "Dia da Mulher": "Homenagens e histórias de mulheres reais.",
    "Sexta-feira Santa": "Feriadão: viagem, chocolate e bacalhau.",
    "Páscoa": "Ovos, preços e receitas: comparativos engajam.",
    "Tiradentes": "Feriado: plano de folga e meme de feriado prolongado.",
    "Dia do Trabalho": "Rotina, salário e \"quem trabalha hoje?\".",
    "Dia das Mães": "Homenagens e presentes: um dos dias mais fortes do ano.",
    "Dia dos Namorados": "Casais, solteiros e presentes: humor vale ouro.",
    "São João": "Festa junina: comidas, quadrilha e looks.",
    "Dia dos Pais": "Homenagens e \"pai é tudo igual\".",
    "Independência": "Feriado e desfiles; cuidado com política.",
    "Dia das Crianças": "Nostalgia de brinquedos e fotos de infância.",
    "Halloween": "Fantasias e terror: trends de maquiagem e filmes.",
    "Finados": "Feriado: tom respeitoso; homenagens.",
    "Consciência Negra": "Histórias, referências e representatividade.",
    "Black Friday": "Ofertas, golpes e \"vale a pena?\": comece dias antes.",
    "Natal": "Presentes, ceia e amigo secreto.",
    "Réveillon": "Simpatias, looks brancos e retrospectiva.",
    "Eleições (1º turno)": "Apuração em tempo real domina o X: prepare-se para a noite.",
    "Eleições (2º turno)": "Resultado e reações: o assunto do ano.",
}


def easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-ésimo dia da semana do mês (weekday 0 = segunda, 6 = domingo); n = -1 é o último."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = (date(year + (month == 12), month % 12 + 1, 1)) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def dates_of(year: int) -> list[tuple[date, str]]:
    pascoa = easter(year)
    out = [
        (date(year, 1, 1), "Ano Novo"),
        (pascoa - timedelta(days=47), "Carnaval"),
        (pascoa - timedelta(days=46), "Quarta de Cinzas"),
        (date(year, 3, 8), "Dia da Mulher"),
        (pascoa - timedelta(days=2), "Sexta-feira Santa"),
        (pascoa, "Páscoa"),
        (date(year, 4, 21), "Tiradentes"),
        (date(year, 5, 1), "Dia do Trabalho"),
        (nth_weekday(year, 5, 6, 2), "Dia das Mães"),
        (date(year, 6, 12), "Dia dos Namorados"),
        (date(year, 6, 24), "São João"),
        (nth_weekday(year, 8, 6, 2), "Dia dos Pais"),
        (date(year, 9, 7), "Independência"),
        (date(year, 10, 12), "Dia das Crianças"),
        (date(year, 10, 31), "Halloween"),
        (date(year, 11, 2), "Finados"),
        (date(year, 11, 20), "Consciência Negra"),
        (nth_weekday(year, 11, 4, -1), "Black Friday"),
        (date(year, 12, 25), "Natal"),
        (date(year, 12, 31), "Réveillon"),
    ]
    if year % 2 == 0:
        out += [(nth_weekday(year, 10, 6, 1), "Eleições (1º turno)"), (nth_weekday(year, 10, 6, -1), "Eleições (2º turno)")]
    return sorted(out)


def upcoming(today: date, days: int = 14) -> list[dict[str, Any]]:
    """Datas de hoje até `days` dias à frente (atravessa o ano)."""
    end = today + timedelta(days=days)
    found = [(day, name) for year in {today.year, end.year} for day, name in dates_of(year) if today <= day <= end]
    return [
        {"date": day.isoformat(), "name": name, "in_days": (day - today).days, "tip": TIPS.get(name, ""), "kind": "sazonal"}
        for day, name in sorted(found)
    ]
