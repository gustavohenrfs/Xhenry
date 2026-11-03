"""Aba Métricas: os sinais que o algoritmo do X premia, a partir dos posts que o robô já leu.

Não chama o X nem o Apify: usa as mesmas linhas de `tweet_samples` do dashboard (o "Atualizar dados" traz
tudo junto). O que o X não entrega para ninguém de fora (tempo de leitura, se você respondeu as replies)
não é inventado: volta como indisponível, com a melhor pista no lugar.

Pontos do algoritmo de um post = curtida 0,5 + repost 1 + citação 5 + reply 5 (pesos do "Para você" de 2026, o
Phoenix: multiplicam a chance de cada ação). Salvar saiu da lista de ações do código novo, então não soma ponto
(continua na tela como pista de leitura). Copiar o link (+20) e DM (+5) não aparecem para quem é de fora do X.
"Nota por mil views" é esse total dividido pelas views: mede a qualidade, não o tamanho da conta.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DailyMetric, Folder, TweetSample, XAccount
from app.services import (
    STATUS_LABELS, as_utc, latest_metrics, load_samples, now_utc, period_window, selected_accounts, within,
)

POINTS = {"likes": 0.5, "retweets": 1.0, "quotes": 5.0, "bookmarks": 0.0, "replies": 5.0}
SIGNAL_LABELS = {"replies": "Replies", "bookmarks": "Salvamentos", "quotes": "Citações", "retweets": "Reposts",
                 "likes": "Curtidas"}
# Fase pelo número de seguidores e o KPI que mais importa nela (playbook do guia).
PHASES = [
    (1_000, "0 a 1 mil", "replies", "Replies em contas maiores: 70% do tempo respondendo, 30% postando."),
    (10_000, "1 mil a 10 mil", "replies", "Replies por post no seu texto: siga de volta quem comenta (reply de mútuo vale +20)."),
    (50_000, "10 mil a 50 mil", "quotes", "Citações recebidas: valem 5 reposts."),
    (10 ** 12, "50 mil ou mais", "points", "Nota por mil views: volume sem perder qualidade."),
]
TOP = 5
MIN_DAYS = 14


def points_of(row: Any) -> float:
    return (row.like_count * POINTS["likes"] + row.retweet_count * POINTS["retweets"] + row.quote_count * POINTS["quotes"]
            + row.bookmark_count * POINTS["bookmarks"] + row.reply_count * POINTS["replies"])


def is_own_reply(row: Any) -> bool:
    return (row.text_preview or "").lstrip().startswith("@")


def totals(rows: list) -> dict[str, Any]:
    """Somas e médias de um conjunto de posts (as replies que a própria conta mandou não contam como post)."""
    posts = [row for row in rows if not is_own_reply(row)]
    t = {"posts": len(posts), "views": 0, "likes": 0, "retweets": 0, "replies": 0, "quotes": 0, "bookmarks": 0,
         "points": 0.0, "own_replies": len(rows) - len(posts)}
    for row in posts:
        t["views"] += row.view_count
        t["likes"] += row.like_count
        t["retweets"] += row.retweet_count
        t["replies"] += row.reply_count
        t["quotes"] += row.quote_count
        t["bookmarks"] += row.bookmark_count
        t["points"] += points_of(row)
    n, views = t["posts"], t["views"]
    per_post = (lambda v: round(v / n, 2)) if n else (lambda v: None)
    per_k = (lambda v: round(v * 1000 / views, 2)) if views else (lambda v: None)
    t.update(
        bookmarks_per_post=per_post(t["bookmarks"]), replies_per_post=per_post(t["replies"]),
        quotes_per_post=per_post(t["quotes"]), retweets_per_post=per_post(t["retweets"]),
        points_per_post=per_post(t["points"]), views_per_post=per_post(t["views"]),
        bookmarks_per_k=per_k(t["bookmarks"]), replies_per_k=per_k(t["replies"]), quotes_per_k=per_k(t["quotes"]),
        points_per_k=per_k(t["points"]),
        # Salvar ÷ curtir: quanto do público guarda em vez de só aplaudir (pista de leitura de verdade).
        save_rate=round(t["bookmarks"] * 100 / t["likes"], 1) if t["likes"] else None,
    )
    t["points"] = round(t["points"], 1)
    return t


def phase_of(followers: int | None) -> dict[str, Any] | None:
    if followers is None:
        return None
    for limit, label, kpi, tip in PHASES:
        if followers < limit:
            return {"label": label, "kpi": kpi, "tip": tip}
    return None


def followers_series(db: Session, accounts: list, start: date, end: date) -> dict[str, Any]:
    """Seguidores por dia (soma das contas, cada uma com a última leitura até aquele dia) e o saldo de cada conta
    no período. Sem leitura nenhuma, tudo None."""
    ids = [account.id for account in accounts]
    readings: dict[int, list[tuple[date, int]]] = {}
    if ids:
        for account_id, day, count in db.execute(
            select(DailyMetric.account_id, DailyMetric.metric_date, DailyMetric.followers_count)
            .where(DailyMetric.account_id.in_(ids), DailyMetric.followers_count.is_not(None), DailyMetric.metric_date <= end)
            .order_by(DailyMetric.account_id, DailyMetric.metric_date)
        ):
            readings.setdefault(account_id, []).append((day, int(count)))
    if not readings:
        return {"series": [], "gained": None, "by_account": {}}
    series = []
    day = start
    while day <= end:
        known = [next((c for d, c in reversed(rows) if d <= day), None) for rows in readings.values()]
        known = [c for c in known if c is not None]
        series.append({"date": day.isoformat(), "total": sum(known) if known else None})
        day += timedelta(days=1)
    by_account = {}
    for account_id, rows in readings.items():
        inside = [c for d, c in rows if start <= d <= end]
        before = [c for d, c in rows if d < start]
        if inside:
            by_account[account_id] = inside[-1] - (before[-1] if before else inside[0])
    gained = sum(by_account.values()) if by_account else None
    return {"series": series, "gained": gained, "by_account": by_account}


def vanity(now: dict[str, Any], rows: list, followers: dict[str, Any]) -> list[dict[str, Any]]:
    """As métricas de vaidade do guia, com os números de verdade: o que parece bom e o algoritmo quase ignora."""
    posts = [row for row in rows if not is_own_reply(row)]
    interactions = now["likes"] + now["retweets"] + now["replies"] + now["quotes"] + now["bookmarks"]
    like_share = round(now["likes"] * 100 / interactions) if interactions else None
    like_points = round(now["likes"] * POINTS["likes"] * 100 / now["points"]) if now["points"] else None
    silent = [row for row in posts if row.view_count and not row.reply_count and not row.bookmark_count and not row.quote_count]
    silent_pct = round(len(silent) * 100 / len(posts)) if posts else None
    total_followers = followers["series"][-1]["total"] if followers["series"] else None
    ratio = round(now["retweets"] / now["quotes"], 1) if now["quotes"] else None
    return [
        {"key": "likes", "title": "Curtidas", "value": now["likes"], "fact": (
            f"São {like_share}% das interações, mas só {like_points}% dos pontos: cada uma vale 0,5." if like_share is not None
            else "Cada curtida vale 0,5: é o sinal mais fácil e o mais barato."), "fix": "Troque pedido de curtida por pergunta que faça responder."},
        {"key": "silent", "title": "Views sem conversa", "value": len(silent), "unit": "posts", "fact": (
            f"{silent_pct}% dos posts tiveram views e nenhuma reply, salvamento ou citação." if silent_pct is not None
            else "Post visto e ignorado piora o alcance dos próximos."), "fix": "Termine com pergunta, enquete ou desafio."},
        {"key": "followers", "title": "Seguidores", "value": total_followers, "fact": (
            f"Saldo no período: {followers['gained']:+d}. Mil seguidores que conversam valem mais que 100 mil parados."
            if followers["gained"] is not None else "Mil seguidores que conversam valem mais que 100 mil parados."),
         "fix": "Olhe a nota por mil views, não só o número de seguidores."},
        {"key": "retweets", "title": "Repost sem comentário", "value": now["retweets"], "fact": (
            f"Para cada citação, {str(ratio).replace('.', ',')} reposts puros. A citação vale 5 vezes mais." if ratio is not None
            else "A citação vale +5, cinco vezes o repost puro (+1)."), "fix": "Na sua rede, cite com comentário em vez de só repostar."},
    ]


def post_item(account: Any, row: Any) -> dict[str, Any]:
    return {
        "account": account.username, "text": (row.text_preview or "")[:280], "views": row.view_count,
        "likes": row.like_count, "retweets": row.retweet_count, "replies": row.reply_count, "quotes": row.quote_count,
        "bookmarks": row.bookmark_count, "points": round(points_of(row), 1), "posted_at": row.posted_at.isoformat(),
        "url": f"https://x.com/{account.username}/status/{row.tweet_id}",
    }


def algo_metrics(db: Session, owner_id: int, period: str, folder_id: int | None) -> dict[str, Any]:
    start, end = period_window(period)
    if folder_id is not None and db.scalar(select(Folder.id).where(Folder.id == folder_id, Folder.owner_id == owner_id)) is None:
        raise HTTPException(404, "Pasta não encontrada.")
    accounts = selected_accounts(db, owner_id, folder_id)
    span = (end - start).days + 1
    is_total = period.strip().lower() == "total"
    since = start if is_total else min(start - timedelta(days=span), end - timedelta(days=MIN_DAYS - 1))
    loaded = load_samples(db, accounts, since)
    current = within(loaded, start, end)
    previous = [] if is_total else within(loaded, start - timedelta(days=span), start - timedelta(days=1))
    cur_rows = [row for _, row in current]
    now = totals(cur_rows)
    before = totals([row for _, row in previous]) if not is_total else None

    # Série por dia (pelo menos 14 dias; no "Total", do primeiro post).
    chart_start = min(start, end - timedelta(days=MIN_DAYS - 1))
    if is_total and loaded:
        chart_start = max(start, min(min(day for _, _, day in loaded), end - timedelta(days=MIN_DAYS - 1)))
    days: dict[date, dict[str, Any]] = {}
    day = chart_start
    while day <= end:
        days[day] = {"date": day.isoformat(), "posts": 0, "views": 0, "replies": 0, "bookmarks": 0, "quotes": 0,
                     "retweets": 0, "likes": 0, "points": 0.0}
        day += timedelta(days=1)
    for _, row, local_day in loaded:
        bucket = days.get(local_day)
        if bucket is None or is_own_reply(row):
            continue
        bucket["posts"] += 1
        bucket["views"] += row.view_count
        bucket["replies"] += row.reply_count
        bucket["bookmarks"] += row.bookmark_count
        bucket["quotes"] += row.quote_count
        bucket["retweets"] += row.retweet_count
        bucket["likes"] += row.like_count
        bucket["points"] += points_of(row)
    series = []
    for bucket in days.values():
        bucket["points"] = round(bucket["points"], 1)
        bucket["points_per_k"] = round(bucket["points"] * 1000 / bucket["views"], 2) if bucket["views"] else None
        series.append(bucket)

    # De onde vêm os pontos (o que mais pesa na conta de cada um).
    mix = [{"key": key, "label": SIGNAL_LABELS[key], "points": round(now[key] * POINTS[key], 1), "count": now[key],
            "weight": POINTS[key]} for key in ("replies", "quotes", "retweets", "likes", "bookmarks") if POINTS[key]]

    # Hora do post × pontos por post (média): quando o algoritmo mais premia as suas contas.
    hours = [{"hour": h, "posts": 0, "points": 0.0} for h in range(24)]
    for _, row in current:
        if is_own_reply(row):
            continue
        slot = hours[row.local.hour]
        slot["posts"] += 1
        slot["points"] += points_of(row)
    for slot in hours:
        slot["avg"] = round(slot["points"] / slot["posts"], 1) if slot["posts"] else None
        slot["points"] = round(slot["points"], 1)

    latest = latest_metrics(db, [account.id for account in accounts])
    follow = followers_series(db, accounts, chart_start, end)
    follow_period = followers_series(db, accounts, start, end) if chart_start != start else follow
    # Ritmo: posts por conta por dia (o guia fala de 3 a 5 originais por dia, uns 95 por semana com replies).
    active = max(1, len(accounts))
    now["posts_per_day"] = round(now["posts"] / span / active, 2) if not is_total else round(
        now["posts"] / max(1, len(series)) / active, 2)
    if before is not None:
        before["posts_per_day"] = round(before["posts"] / span / active, 2)
    by_account: dict[int, list] = {}
    for account, row in current:
        by_account.setdefault(account.id, []).append(row)
    prev_by_account: dict[int, list] = {}
    for account, row in previous:
        prev_by_account.setdefault(account.id, []).append(row)
    spark_start = end - timedelta(days=MIN_DAYS - 1)
    sparks: dict[int, list[float]] = {account.id: [0.0] * MIN_DAYS for account in accounts}
    for account, row, local_day in loaded:
        if spark_start <= local_day <= end and not is_own_reply(row):
            sparks[account.id][(local_day - spark_start).days] += points_of(row)
    rows = []
    for account in accounts:
        mine = totals(by_account.get(account.id, []))
        prior = totals(prev_by_account.get(account.id, [])) if not is_total else None
        followers = latest[account.id].followers_count if account.id in latest else None
        status = account.last_health_status.value if account.last_health_status else None
        rows.append(mine | {
            "id": account.id, "username": account.username, "display_name": account.display_name,
            "folder": account.folder.name if account.folder else "", "color": account.folder.color if account.folder else None,
            "followers": followers, "phase": phase_of(followers), "status": status,
            "status_label": STATUS_LABELS.get(status or "", "Sem verificação"),
            "points_per_k_prev": prior["points_per_k"] if prior else None,
            "followers_gained": follow_period["by_account"].get(account.id),
            "spark": [round(v, 1) for v in sparks.get(account.id, [])],
        })
    rows.sort(key=lambda r: (-(r["points"] or 0), r["username"]))

    posts = [(account, row) for account, row in current if not is_own_reply(row)]

    def top(key) -> list[dict[str, Any]]:
        ranked = sorted(posts, key=lambda pair: -key(pair[1]))
        return [post_item(account, row) for account, row in ranked[:TOP] if key(row) > 0]

    return {
        "period": period, "start": start.isoformat(), "end": end.isoformat(),
        "totals": now, "previous": before,
        "series": series, "mix": mix, "hours": hours, "accounts": rows,
        "top": {
            "bookmarks": top(lambda r: r.bookmark_count), "replies": top(lambda r: r.reply_count),
            "quotes": top(lambda r: r.quote_count), "points_per_k": top(
                lambda r: points_of(r) * 1000 / r.view_count if r.view_count >= 500 else 0),
        },
        "weights": POINTS,
        "followers": {"series": follow["series"], "gained": follow_period["gained"]},
        "vanity": vanity(now, cur_rows, follow_period | {"series": follow["series"]}),
        # O que o X não entrega para quem está de fora: a tela explica em vez de inventar.
        "available": {"dwell": False, "own_replies": now["own_replies"] > 0 or any(
            is_own_reply(row) for _, row, _ in loaded)},
    }


# ---------- Responder agora ----------
REPLY_OPEN_MIN = 30     # responder os comentários antes de o post fazer 30 min
REPLY_WINDOW_MIN = 60   # posts mais velhos que 1 h saem da lista


def reply_watch(db: Session, owner_id: int, folder_id: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Posts da última hora que receberam comentário, para responder antes dos 30 min.

    Só lê `tweet_samples` (o "Atualizar dados" traz o reply_count novo). Replies que a própria conta mandou não
    contam como post. Abertos primeiro, quem fecha antes no topo; depois os que já passaram dos 30 min."""
    now = now or now_utc()
    accounts = {a.id: a for a in selected_accounts(db, owner_id, folder_id)}
    if not accounts:
        return {"items": [], "posts": 0, "with_replies": 0, "read_at": None,
                "open_min": REPLY_OPEN_MIN, "window_min": REPLY_WINDOW_MIN}
    rows = db.execute(
        select(TweetSample.account_id, TweetSample.tweet_id, TweetSample.posted_at, TweetSample.reply_count,
               TweetSample.text_preview, TweetSample.collected_at)
        .where(TweetSample.account_id.in_(accounts), TweetSample.posted_at >= now - timedelta(minutes=REPLY_WINDOW_MIN))
    ).all()
    posts = [row for row in rows if not is_own_reply(row)]
    items = []
    for row in posts:
        if not row.reply_count:
            continue
        age = (now - as_utc(row.posted_at)).total_seconds() / 60
        account = accounts[row.account_id]
        items.append({
            "account_id": account.id, "username": account.username, "display_name": account.display_name,
            "tweet_id": row.tweet_id, "replies": row.reply_count, "text": row.text_preview or "",
            "posted_at": as_utc(row.posted_at).isoformat(), "age_min": int(age),
            "left_min": max(0, int(REPLY_OPEN_MIN - age)), "open": age < REPLY_OPEN_MIN,
        })
    items.sort(key=lambda item: (not item["open"], item["age_min"] * (-1 if item["open"] else 1)))
    read = db.scalar(select(func.max(XAccount.collected_at)).where(XAccount.id.in_(accounts)))
    return {"items": items, "posts": len(posts), "with_replies": len(items),
            "read_at": as_utc(read).isoformat() if read else None,
            "open_min": REPLY_OPEN_MIN, "window_min": REPLY_WINDOW_MIN}
