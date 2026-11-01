"""Aba Postar: textos guardados por conta para o lote manual.

No Radar o usuário toca "Guardar p/ conta" e escolhe a conta; aqui cada conta ativa vira um cartão com o
próximo texto da fila e o botão Copiar. O painel nunca posta: o usuário cola no X (multilogin) e marca
"Postei". O lote é o que foi marcado nas últimas BATCH_WINDOW horas.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Folder, PostDraft, XAccount
from app.radar_sources import norm
from app.services import as_utc, now_utc, owned_account, selected_accounts

MAX_QUEUE = 20                      # textos na fila de uma conta
MAX_TEXT = 4000                     # post longo do X Premium cabe; o comum é 280
BATCH_WINDOW = timedelta(hours=3)   # "Postei" conta no lote atual por 3 h
KEEP_POSTED = timedelta(days=30)    # histórico do que já foi postado


def clean_text(text: str) -> str:
    """Tira espaços das pontas e linhas vazias sobrando, mas mantém as quebras de linha do post."""
    lines = [line.rstrip() for line in (text or "").strip().splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def same_key(text: str) -> str:
    """Chave para achar texto repetido (sem caixa, acento e pontuação); só emoji compara pelo texto."""
    return norm(text) or text


def owned_draft(db: Session, owner_id: int, draft_id: int) -> PostDraft:
    draft = db.scalar(select(PostDraft).where(PostDraft.id == draft_id, PostDraft.owner_id == owner_id))
    if draft is None:
        raise HTTPException(404, "Texto não encontrado.")
    return draft


def board(db: Session, owner_id: int) -> dict[str, Any]:
    """Todas as contas ativas do usuário, com a fila e o último "Postei" do lote atual."""
    now = now_utc()
    db.execute(delete(PostDraft).where(PostDraft.owner_id == owner_id, PostDraft.posted_at.is_not(None),
                                       PostDraft.posted_at < now - KEEP_POSTED))
    db.commit()
    accounts = selected_accounts(db, owner_id, None)
    folders = {f.id: f for f in db.scalars(select(Folder).where(Folder.owner_id == owner_id))}
    drafts = db.scalars(select(PostDraft).where(PostDraft.owner_id == owner_id).order_by(PostDraft.id)).all()
    queue: dict[int, list[dict[str, Any]]] = {}
    posted: dict[int, dict[str, Any]] = {}
    for draft in drafts:
        if draft.posted_at is None:
            queue.setdefault(draft.account_id, []).append({"id": draft.id, "text": draft.text})
            continue
        at = as_utc(draft.posted_at)
        if now - at <= BATCH_WINDOW and (draft.account_id not in posted or at >= posted[draft.account_id]["at"]):
            posted[draft.account_id] = {"id": draft.id, "text": draft.text, "at": at}
    rows = []
    for account in sorted(accounts, key=lambda a: ((folders[a.folder_id].name if a.folder_id in folders else "").lower(),
                                                   a.username.lower())):
        folder = folders.get(account.folder_id)
        done = posted.get(account.id)
        rows.append({
            "id": account.id, "username": account.username, "display_name": account.display_name,
            "folder_id": account.folder_id, "folder_name": folder.name if folder else "",
            "folder_color": folder.color if folder else None,
            "queue": queue.get(account.id, []),
            "posted": {"id": done["id"], "text": done["text"], "posted_at": done["at"].isoformat()} if done else None,
        })
    return {"accounts": rows, "batch": {"done": len([r for r in rows if r["posted"]]), "total": len(rows),
                                        "hours": int(BATCH_WINDOW.total_seconds() // 3600)}}


def save(db: Session, owner_id: int, account_id: int, text: str) -> dict[str, Any]:
    account = owned_account(db, owner_id, account_id)
    text = clean_text(text)
    if not text:
        raise HTTPException(400, "Escreva o texto do post.")
    if len(text) > MAX_TEXT:
        raise HTTPException(400, f"Texto acima de {MAX_TEXT} caracteres.")
    key = same_key(text)
    waiting = db.scalars(select(PostDraft).where(PostDraft.owner_id == owner_id, PostDraft.posted_at.is_(None))).all()
    mine = [d for d in waiting if d.account_id == account.id]
    if any(same_key(d.text) == key for d in mine):
        raise HTTPException(409, f"Esse texto já está na fila de @{account.username}.")
    if len(mine) >= MAX_QUEUE:
        raise HTTPException(400, f"A fila de @{account.username} já tem {MAX_QUEUE} textos. Poste ou apague um.")
    others = {d.account_id for d in waiting if d.account_id != account.id and same_key(d.text) == key}
    elsewhere = list(db.scalars(select(XAccount.username).where(XAccount.id.in_(others)))) if others else []
    draft = PostDraft(owner_id=owner_id, account_id=account.id, text=text)
    db.add(draft)
    db.commit()
    return {"id": draft.id, "account_id": account.id, "username": account.username, "text": draft.text,
            "position": len(mine) + 1, "elsewhere": sorted(elsewhere)}


def update(db: Session, owner_id: int, draft_id: int, posted: bool | None, text: str | None) -> dict[str, Any]:
    draft = owned_draft(db, owner_id, draft_id)
    if text is not None:
        text = clean_text(text)
        if not text:
            raise HTTPException(400, "Escreva o texto do post.")
        draft.text = text[:MAX_TEXT]
    if posted is not None:
        draft.posted_at = now_utc() if posted else None
    db.commit()
    return {"id": draft.id, "text": draft.text, "posted_at": as_utc(draft.posted_at).isoformat() if draft.posted_at else None}


def remove(db: Session, owner_id: int, draft_id: int) -> None:
    db.delete(owned_draft(db, owner_id, draft_id))
    db.commit()



# ---------- Como os textos se saíram ----------
# Cada "Postei" é ligado ao post real que o robô leu da mesma conta: texto igual ou quase (sem caixa, acento,
# pontuação e emoji) e publicado entre 6 h antes e 1 h depois do clique. As frases iguais em contas diferentes
# somam numa linha só. Campeã = acima da mediana das frases do lote e acima das suas views normais (ou com
# conversa: CHAMPION_REPLIES respostas em média).
MATCH_BEFORE = timedelta(hours=6)
MATCH_AFTER = timedelta(hours=1)
RESULTS_DAYS = 30
CHAMPION_REPLIES = 20


def _words(text: str) -> set[str]:
    return set(norm(text).split())


def same_post(draft_text: str, tweet_text: str) -> bool:
    a, b = norm(draft_text), norm(tweet_text)
    if not a or not b:
        return False
    if a == b or a.startswith(b[:60]) or b.startswith(a[:60]):
        return True
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / max(1, len(wa | wb)) >= .75


def _median(values: list[float]) -> float:
    values = sorted(values)
    return values[len(values) // 2] if values else 0


def results(db: Session, owner_id: int, folder_id: int | None = None) -> dict[str, Any]:
    from app.algo_metrics import is_own_reply
    from app.models import TweetSample
    from app.radar_hot import LABELS, format_of

    now = now_utc()
    accounts = {a.id: a for a in selected_accounts(db, owner_id, folder_id)}
    empty = {"phrases": [], "pending": 0, "normal_views": 0, "days": RESULTS_DAYS}
    if not accounts:
        return empty
    drafts = db.scalars(select(PostDraft).where(
        PostDraft.owner_id == owner_id, PostDraft.posted_at.is_not(None), PostDraft.account_id.in_(accounts),
        PostDraft.posted_at >= now - timedelta(days=RESULTS_DAYS)).order_by(PostDraft.posted_at)).all()
    if not drafts:
        return empty
    oldest = min(as_utc(d.posted_at) for d in drafts) - MATCH_BEFORE
    tweets = db.execute(select(
        TweetSample.account_id, TweetSample.tweet_id, TweetSample.posted_at, TweetSample.text_preview,
        TweetSample.view_count, TweetSample.reply_count, TweetSample.like_count,
    ).where(TweetSample.account_id.in_(accounts), TweetSample.posted_at >= oldest)).all()
    by_account: dict[int, list[Any]] = {}
    for row in tweets:
        if row.text_preview and not is_own_reply(row):
            by_account.setdefault(row.account_id, []).append(row)
    normal = db.execute(select(TweetSample.view_count).where(
        TweetSample.account_id.in_(accounts), TweetSample.posted_at >= now - timedelta(days=90))).scalars().all()
    normal_views = _median([v or 0 for v in normal])

    used: set[str] = set()
    phrases: dict[str, dict[str, Any]] = {}
    pending = 0
    for draft in drafts:
        at = as_utc(draft.posted_at)
        candidates = [t for t in by_account.get(draft.account_id, [])
                      if t.tweet_id not in used and at - MATCH_BEFORE <= as_utc(t.posted_at) <= at + MATCH_AFTER
                      and same_post(draft.text, t.text_preview)]
        hit = min(candidates, key=lambda t: abs((as_utc(t.posted_at) - at).total_seconds()), default=None)
        key = same_key(draft.text)
        phrase = phrases.setdefault(key, {"text": draft.text, "posts": 0, "matched": 0, "views": 0, "replies": 0,
                                          "likes": 0, "best": None, "last_at": None})
        phrase["posts"] += 1
        phrase["last_at"] = at.isoformat()
        if hit is None:
            pending += 1
            continue
        used.add(hit.tweet_id)
        account = accounts[draft.account_id]
        views, replies = hit.view_count or 0, hit.reply_count or 0
        phrase["matched"] += 1
        phrase["views"] += views
        phrase["replies"] += replies
        phrase["likes"] += hit.like_count or 0
        if phrase["best"] is None or views > phrase["best"]["views"]:
            phrase["best"] = {"username": account.username, "views": views, "replies": replies,
                              "url": f"https://x.com/{account.username}/status/{hit.tweet_id}"}
    rows = []
    for phrase in phrases.values():
        matched = phrase["matched"]
        fmt = format_of(phrase["text"])
        phrase |= {"avg_views": round(phrase["views"] / matched) if matched else None,
                   "avg_replies": round(phrase["replies"] / matched) if matched else None,
                   "fmt": fmt, "fmt_label": LABELS[fmt][0], "champion": False}
        rows.append(phrase)
    seen = [r for r in rows if r["matched"]]
    middle = _median([r["avg_views"] for r in seen])
    for row in seen:
        row["champion"] = row["avg_views"] >= middle and (row["avg_views"] >= normal_views or row["avg_replies"] >= CHAMPION_REPLIES)
    rows.sort(key=lambda r: (r["matched"] == 0, -(r["avg_views"] or 0)))
    return {"phrases": rows, "pending": pending, "normal_views": int(normal_views), "days": RESULTS_DAYS}
