from __future__ import annotations
from fastapi import APIRouter, Depends, Form, File, UploadFile, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
import app.models as models

router = APIRouter()

@app.get("/api/suggestions")
def get_suggestions(
    period: str = Query(default="7d"),
    folder_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    start, end = period_window(period)
    return build_suggestions(db, selected_accounts(db, folder_id), start, end)


def get_settings(db: Session) -> AppSetting:
    row = db.get(AppSetting, 1)
    if row is None:
        row = AppSetting(id=1, impression_goal=500)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def telegram_creds(db: Session) -> tuple[str, str]:
    row = get_settings(db)
    token = (row.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (row.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat


def xai_key(db: Session) -> str:
    row = get_settings(db)
    return (row.xai_api_key or os.getenv("XAI_API_KEY") or "").strip()


def send_telegram(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        raise HTTPException(503, "Configure o token do bot e o Chat ID em Configurações.")
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            response = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Falha de rede ao falar com o Telegram.") from exc
    if response.status_code != 200:
        detail = ""
        try:
            detail = str(response.json().get("description") or "")
        except ValueError:
            detail = ""
        raise HTTPException(502, detail or "O Telegram recusou a mensagem.")


def viral_message(name: str, username: str, gain: int, when: str, likes: int, reposts: int, link: str) -> str:
    return (
        "🚀 Post viralizando!\n"
        f"👤 Conta: {name} (@{username})\n"
        f"👁 Ganho de impressao no post: {gain}\n"
        f"⏰ Horário do post: {when}\n"
        f"💬 Engajamento: {likes + reposts}\n"
        f"🔗 Link do Post: {link}"
    )


def try_send(db: Session, key: str, kind: str, text: str) -> bool:
    token, chat = telegram_creds(db)
    if not token or not chat:
        return False
    if db.scalar(select(AlertSent).where(AlertSent.dedupe_key == key)):
        return False
    try:
        send_telegram(token, chat, text)
    except HTTPException as exc:
        log.warning("telegram %s não enviado: %s", kind, exc.detail)
        return False
    db.add(AlertSent(dedupe_key=key, kind=kind, detail=text[:500], sent_at=now_utc()))
    db.commit()
    return True


def dispatch_alerts(
    db: Session,
    account: XAccount,
    signals: dict[str, Any],
    previous_status: HealthStatus | None,
    prior_imps: dict[str, int | None],
) -> None:
    settings = get_settings(db)
    goal = int(settings.impression_goal or 500)
    name = account.display_name or account.username
    tz = zone(account.timezone)
    local_now = datetime.now(tz)
    for raw in signals["timeline"]:
        metrics = raw.get("public_metrics") if isinstance(raw.get("public_metrics"), dict) else {}
        current = metric_value(metrics, "impression_count")
        if current is None or current < goal:
            continue
        previous = prior_imps.get(str(raw["id"]))
        gain = current - int(previous or 0)
        posted = parse_time(raw.get("created_at")) or now_utc()
        when = as_utc(posted).astimezone(tz).strftime("%d/%m/%Y %H:%M")
        likes = int(metrics.get("like_count") or 0)
        reposts = int(metrics.get("retweet_count") or 0)
        link = f"https://x.com/{account.username}/status/{raw['id']}"
        try_send(
            db,
            f"viral:{account.id}:{raw['id']}",
            "viral",
            viral_message(name, account.username, gain, when, likes, reposts, link),
        )
    status = account.last_health_status
    if status in {HealthStatus.SEARCH_BAN, HealthStatus.GHOST_BAN} and status != previous_status:
        label = STATUS_LABELS[status.value]
        try_send(
            db,
            f"ban:{account.id}:{status.value}:{local_now.date().isoformat()}",
            "ban",
            f"⚠️ Possível restrição\n👤 Conta: {name} (@{account.username})\nStatus: {label}\nO alcance pode ter sido cortado.",
        )
    sampled = signals["timeline"]
    if not sampled:
        return
    avg = round(sum(engagement_of(row.get("public_metrics") or {}) for row in sampled) / len(sampled))
    baseline = db.scalar(
        select(DayBaseline).where(DayBaseline.account_id == account.id, DayBaseline.metric_date == local_now.date())
    )
    if baseline is None:
        baseline = DayBaseline(account_id=account.id, metric_date=local_now.date())
        db.add(baseline)
    if local_now.hour < 18:
        baseline.day_avg = avg
        db.commit()
        return
    if baseline.day_avg and avg < baseline.day_avg * 0.7 and not baseline.drop_alerted:
        drop = round((1 - avg / baseline.day_avg) * 100)
        sent = try_send(
            db,
            f"drop:{account.id}:{local_now.date().isoformat()}",
            "drop",
            f"⚠️ Queda de engajamento\n👤 Conta: {name} (@{account.username})\n📉 Do dia para a noite: -{drop}%\nPode ser sombra no alcance. Confira o status da conta.",
        )
        baseline.drop_alerted = sent or baseline.drop_alerted
    db.commit()


def settings_payload(row: AppSetting) -> dict[str, Any]:
    return {
        "telegram_token_set": bool(row.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")),
        "telegram_chat_id": row.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID") or "",
        "impression_goal": row.impression_goal,
        "xai_key_set": bool(row.xai_api_key or os.getenv("XAI_API_KEY")),
    }


def grok_text(body: dict) -> str:
    parts: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("content"), str):
            parts.append(item["content"])
        for chunk in item.get("content") or []:
            if isinstance(chunk, dict) and chunk.get("text"):
                parts.append(str(chunk["text"]))
    if not parts and body.get("choices"):
        message = body["choices"][0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(part.strip() for part in parts if part and part.strip())


def ask_grok(key: str, caption: str, image_uri: str | None) -> str:
    prompt = (
        "Avalie se este post do X corre risco de restrição da plataforma.\n"
        f"Legenda:\n{caption}\n\n"
        "Se houver imagem, diga se ela sugere nudez, violência gráfica, sangue, ameaça "
        "ou outro motivo comum de limite no X. Responda em português, em um parágrafo curto: "
        "veredito (liberado, atenção ou risco alto) e o motivo em uma frase. "
        "Não descreva conteúdo sexual de forma gráfica e não ensine a burlar a moderação."
    )
    content: list[dict[str, Any]] = []
    if image_uri:
        content.append({"type": "input_image", "image_url": image_uri, "detail": "high"})
    content.append({"type": "input_text", "text": prompt})
    model = os.getenv("XAI_MODEL", "grok-4.6")
    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        response = client.post(
            "https://api.x.ai/v1/responses",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "input": [{"role": "user", "content": content}]},
        )
        if response.status_code >= 400:
            parts: list[dict[str, Any]] = []
            if image_uri:
                parts.append({"type": "image_url", "image_url": {"url": image_uri}})
            parts.append({"type": "text", "text": prompt})
            response = client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": parts}]},
            )
    if response.status_code >= 400:
        raise HTTPException(502, "A xAI recusou a análise. Confira a chave e o modelo.")
    try:
        text = grok_text(response.json())
    except ValueError as exc:
        raise HTTPException(502, "A xAI não devolveu um veredito legível.") from exc
    return text or "A análise não devolveu texto."


@app.put("/api/settings")
def write_settings(payload: SettingsIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = get_settings(db)
    if payload.telegram_token:
        row.telegram_token = payload.telegram_token.strip()
    if payload.telegram_chat_id is not None:
        row.telegram_chat_id = payload.telegram_chat_id.strip() or None
    if payload.impression_goal is not None:
        row.impression_goal = payload.impression_goal
    if payload.xai_api_key:
        row.xai_api_key = payload.xai_api_key.strip()
    db.commit()
    return settings_payload(row)


@app.post("/api/telegram/test")
def test_telegram(db: Session = Depends(get_db)) -> dict[str, bool]:
    row = get_settings(db)
    when = datetime.now(zone(DEFAULT_TZ)).strftime("%d/%m/%Y %H:%M")
    send_telegram(
        *telegram_creds(db),
        viral_message("Teste", "xhenry", row.impression_goal, when, 1, 1, "https://x.com/xhenry"),
    )
    return {"ok": True}


@app.post("/api/posts/review")
async def review_post(
    caption: str = Form(...),
    media: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    text = caption.strip()
    if not text:
        raise HTTPException(400, "A legenda é obrigatória.")
    if len(text) > 2000:
        raise HTTPException(400, "Legenda acima de 2000 caracteres.")
    key = xai_key(db)
    if not key:
        raise HTTPException(503, "Defina a chave da xAI em Configurações ou em XAI_API_KEY.")
    image_uri = None
    tmp_path = None
    if media is not None and media.filename:
        raw = await media.read()
        if len(raw) > 8_000_000:
            raise HTTPException(400, "Imagem acima de 8 MB.")
        kind = (media.content_type or "").lower()
        if kind not in {"image/jpeg", "image/jpg", "image/png"}:
            raise HTTPException(400, "Use JPG ou PNG. A API de visão não aceita outro formato.")
        suffix = "png" if kind == "image/png" else "jpeg"
        folder = ROOT / "data" / "tmp"
        folder.mkdir(parents=True, exist_ok=True)
        tmp_path = folder / f"{secrets.token_hex(8)}.{suffix}"
        tmp_path.write_bytes(raw)
        image_uri = f"data:image/{suffix};base64,{base64.b64encode(raw).decode()}"
    try:
        verdict = ask_grok(key, text, image_uri)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return {"verdict": verdict}


