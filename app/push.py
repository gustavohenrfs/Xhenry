"""Web Push: os alertas de views do robô chegam no celular e no Windows mesmo com o painel fechado.

O navegador se inscreve pelo sininho (/api/push/subscribe) e guarda a inscrição em
push_subscriptions. O vigia (app/watchdog.py) chama push_new_alerts a cada minuto: o
que o robo.py gravou em alerts_sent (kind=viral) depois de push_last_alert_id vai
para todos os inscritos. A chave VAPID é gerada no primeiro uso e fica em
app_settings; VAPID_PRIVATE_KEY no .env tem prioridade. O service worker
(static/sw.js) recebe a mensagem e mostra a notificação com o logo.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AlertSent, PushSubscription
from app.services import as_utc, fmt_int, get_settings, now_utc, viral_item

log = logging.getLogger("xhenry")
ICON = "/static/img/icon-192.png"
BADGE = "/static/img/badge-96.png"
# Até 5 alertas por rodada; o que passar disso fica para o minuto seguinte (o cursor só anda até o
# último enviado). Alerta mais velho que o TTL (vigia parado, deploy) é pulado em vez de chegar atrasado.
MAX_PER_TICK = 5
TTL_SECONDS = 6 * 3600


class PushUnavailable(Exception):
    """pywebpush não instalado no servidor."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _private_key(db: Session) -> str:
    env = os.getenv("VAPID_PRIVATE_KEY", "").strip()
    if env:
        return env
    settings = get_settings(db)
    if not settings.vapid_private_key:
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        settings.vapid_private_key = _b64url(key.private_numbers().private_value.to_bytes(32, "big"))
        db.commit()
    return settings.vapid_private_key


def public_key(db: Session) -> str:
    """Chave pública VAPID (applicationServerKey do navegador), derivada da privada."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = _private_key(db)
    value = int.from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), "big")
    point = ec.derive_private_key(value, ec.SECP256R1()).public_key()
    return _b64url(point.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


def _claims() -> dict[str, str]:
    # A Apple recusa push sem um "sub" válido (https ou mailto).
    base = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    return {"sub": base if base.startswith("https://") else "mailto:alertas@xhenry.app"}


def send_one(db: Session, sub: PushSubscription, payload: dict[str, Any]) -> str:
    """Envia para um navegador: "ok", "gone" (inscrição morta, apagada aqui) ou "error"."""
    try:
        from pywebpush import WebPushException, webpush
    except ImportError as exc:
        raise PushUnavailable("Instale o pywebpush no servidor (pip install -r requirements.txt).") from exc
    try:
        webpush(
            subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=_private_key(db),
            vapid_claims=_claims(),
            ttl=TTL_SECONDS,
            headers={"Urgency": "high"},
            timeout=10,
        )
        return "ok"
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (404, 410):
            db.delete(sub)
            db.commit()
            return "gone"
        log.warning("push recusado (%s): %s", status, str(exc)[:200])
        return "error"
    except Exception as exc:  # rede fora, timeout
        log.warning("push falhou: %s", str(exc)[:200])
        return "error"


def viral_payload(item: dict[str, Any]) -> dict[str, Any]:
    who = (item["display_name"] + " " if item["display_name"] else "") + "@" + item["username"]
    head = who + (f", {fmt_int(item['views'])} views" if item["views"] is not None else "")
    return {
        "title": f"{item['emoji']} {item['title']}",
        "body": head + (f"\n{item['text']}" if item["text"] else ""),
        "url": item["url"] or "/",
        "tag": f"viral-{item['id']}",
        "icon": ICON,
        "badge": BADGE,
    }


def test_payload() -> dict[str, Any]:
    return {
        "title": "🔥 Teste do XHenry",
        "body": "Notificações ligadas. Cada post que viralizar, bombar, passar de 100K ou de 1M aparece aqui.",
        "url": "/",
        "tag": "xhenry-teste",
        "icon": ICON,
        "badge": BADGE,
    }


def push_new_alerts(db: Session) -> int:
    """Empurra os alertas de views que entraram desde a última rodada. Devolve quantos envios deram certo."""
    settings = get_settings(db)
    newest = db.scalar(select(func.max(AlertSent.id)).where(AlertSent.kind == "viral")) or 0
    cursor = settings.push_last_alert_id
    subs = db.scalars(select(PushSubscription).order_by(PushSubscription.id)).all()
    if cursor is None or not subs or newest <= cursor:
        # Primeira vez ou ninguém inscrito: só marca onde está, para não despejar o histórico depois.
        if cursor != newest and (cursor is None or not subs):
            settings.push_last_alert_id = newest
            db.commit()
        return 0
    oldest_ok = now_utc() - timedelta(seconds=TTL_SECONDS)
    pending = db.scalars(
        select(AlertSent)
        .where(AlertSent.kind == "viral", AlertSent.id > cursor)
        .order_by(AlertSent.id)
    ).all()
    fresh = [row for row in pending if as_utc(row.sent_at) >= oldest_ok]
    rows = fresh[:MAX_PER_TICK]
    payloads = [viral_payload(viral_item(db, row)) for row in rows]
    # Sem nada recente, pula os velhos de uma vez; senão para no último enviado e o resto sai depois.
    settings.push_last_alert_id = rows[-1].id if len(fresh) > MAX_PER_TICK else newest
    db.commit()
    sent = 0
    for sub in subs:
        for payload in payloads:
            result = send_one(db, sub, payload)
            if result == "gone":
                break
            sent += result == "ok"
    return sent
