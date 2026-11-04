"""Testa atores do Apify nas contas reais e mostra qual lê os posts de hoje (com views).

Para cada ator e cada jeito de pedir (busca de hoje, busca sem data, @ da conta, link do perfil) roda uma
execução pequena, espera terminar e conta: itens, posts da própria conta, posts de hoje, se veio o campo de
views, quanto tempo levou, quanto custou (usageTotalUsd) e o status/mensagem do Apify. Jeitos cujo campo
não existe no esquema de entrada do ator são pulados (não gastam).

No VPS (pega o token do admin e as contas do banco, pelo .env do painel):

    python3 scripts/apify_probe.py --actors apidojo~tweet-scraper,apidojo~twitter-scraper-lite,OUTRO~ator

Opções: --handles a,b,c (senão, as 3 primeiras contas ativas do admin), --owner ID (contas/token de outro
usuário), --ways busca_hoje,perfil (só alguns jeitos), --max 50. O token também pode vir de APIFY_TOKEN.
Gasta crédito: cada linha da tabela é uma execução.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import robo  # noqa: E402

API = robo.APIFY_API
WAYS = ("robo", "busca_hoje", "busca", "handles", "perfil", "profiles")
FIELD = {"robo": "searchTerms", "busca_hoje": "searchTerms", "busca": "searchTerms", "handles": "twitterHandles", "perfil": "startUrls", "profiles": "profiles"}
RUN_LIMIT = 280  # segundos por execução


def http(method: str, path: str, token: str, body: Any = None) -> Any:
    req = urllib.request.Request(f"{API}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def input_fields(actor: str, token: str) -> set[str] | None:
    """Campos do esquema de entrada do ator (None = não deu para ler; aí tenta todos os jeitos)."""
    try:
        build = http("GET", f"/acts/{actor}/builds/default", token)["data"]
        schema = build.get("inputSchema") or (build.get("actorDefinition") or {}).get("input")
        if isinstance(schema, str):
            schema = json.loads(schema)
        return set((schema or {}).get("properties", {}))
    except Exception as exc:  # noqa: BLE001
        print(f"  (esquema de {actor} não lido: {exc})")
        return None


def payload(way: str, handles: list[str], day: str, max_items: int) -> dict[str, Any]:
    if way == "robo":  # exatamente o que o robô manda hoje (buscas com OR de até 8 contas e since:)
        return robo.apify_input(handles)
    if way == "busca_hoje":
        terms = [f"from:{h} since:{day}" for h in handles]
    elif way == "busca":
        terms = [f"from:{h}" for h in handles]
    if way in ("busca_hoje", "busca"):
        return {"searchTerms": terms, "maxItems": max_items * len(handles), "sort": "Latest"}
    if way == "profiles":  # eiv/twitter-scraper: a linha do tempo do perfil, não a busca
        return {"profiles": handles, "maxTweetsPerProfile": 20, "maxTotalTweets": 20 * len(handles),
                "includeRetweets": False, "includeReplies": False}
    if way == "handles":
        return {"twitterHandles": handles, "maxItems": max_items * len(handles), "sort": "Latest"}
    return {"startUrls": [f"https://x.com/{h}" for h in handles], "maxItems": max_items * len(handles),
            "sort": "Latest"}


def run(actor: str, token: str, body: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    started = http("POST", f"/acts/{actor}/runs?timeout={RUN_LIMIT}", token, body)["data"]
    run_id = started["id"]
    deadline = time.time() + RUN_LIMIT + 60
    info = started
    while info.get("status") in ("READY", "RUNNING") and time.time() < deadline:
        time.sleep(5)
        info = http("GET", f"/actor-runs/{run_id}", token)["data"]
    items = http("GET", f"/datasets/{info['defaultDatasetId']}/items?clean=1&limit=2000", token) or []
    return info, [i for i in items if isinstance(i, dict)]


def summarize(items: list[dict[str, Any]], handles: list[str], midnight: datetime) -> dict[str, Any]:
    wanted = {h.lower() for h in handles}
    own = [i for i in items if robo.apify_author(i)[0] in wanted and robo.tweet_id_of(i) and not robo.is_repost(i)]
    today = [i for i in own if (robo.parse_created_at(robo.pick(i, "createdAt", "created_at", "postedAt")) or midnight) >= midnight
             and robo.parse_created_at(robo.pick(i, "createdAt", "created_at", "postedAt"))]
    found = {robo.apify_author(i)[0] for i in own}
    views = [robo.tweet_metrics(i)["view_count"] for i in today]
    has_views = any(any(k in i for k in ("viewCount", "views", "view_count")) for i in today) if today else None
    sample = sorted(set().union(*(i.keys() for i in items)))[:40] if items else []
    return {"itens": len(items), "da_conta": len(own), "hoje": len(today), "contas_achadas": len(found),
            "views": sum(views), "tem_views": has_views, "campos": sample,
            "faltaram": sorted(wanted - found)}


def from_db(owner: int | None, count: int) -> tuple[str, list[str]]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(robo.database_url(), row_factory=dict_row) as conn, conn.cursor() as cur:
        if owner is None:
            cur.execute("SELECT id FROM users WHERE is_admin IS TRUE ORDER BY id LIMIT 1")
            owner = cur.fetchone()["id"]
        cur.execute("SELECT apify_token FROM user_settings WHERE owner_id = %s", (owner,))
        row = cur.fetchone() or {}
        cur.execute("""SELECT username FROM x_accounts WHERE owner_id = %s AND is_active IS TRUE
                       AND COALESCE(is_archived, FALSE) IS FALSE ORDER BY id LIMIT %s""", (owner, count))
        return (row.get("apify_token") or "").strip(), [r["username"] for r in cur.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--actors", default=f"{robo.APIFY_ACTOR_DEFAULT},{robo.APIFY_UNLIMITED}")
    parser.add_argument("--handles", default="")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--owner", type=int)
    parser.add_argument("--ways", default=",".join(WAYS))
    parser.add_argument("--max", type=int, default=50)
    parser.add_argument("--json", help="salva o resultado completo neste arquivo")
    args = parser.parse_args()

    robo.load_env(Path(os.getenv("ROBO_ENV_FILE") or ROOT / ".env"))
    token = (os.getenv("APIFY_TOKEN") or "").strip()
    handles = [h.strip().lstrip("@") for h in args.handles.split(",") if h.strip()]
    if not token or not handles:
        db_token, db_handles = from_db(args.owner, args.count)
        token = token or db_token
        handles = handles or db_handles
    if not token:
        print("Sem token do Apify (APIFY_TOKEN ou Configurações do painel).")
        return 2
    if not handles:
        print("Nenhuma conta: passe --handles a,b,c.")
        return 2
    midnight = robo.today_start_br()
    day = midnight.date().isoformat()
    actors = [a.strip().replace("/", "~") for a in args.actors.split(",") if a.strip()]
    ways = [w for w in args.ways.split(",") if w in WAYS]
    print(f"Contas: {', '.join('@' + h for h in handles)} — posts depois de {midnight.isoformat()}\n")

    results = []
    for actor in actors:
        fields = input_fields(actor, token)
        if fields is not None:
            print(f"{actor}: campos de entrada = {', '.join(sorted(fields)) or '(nenhum)'}")
        for way in ways:
            row: dict[str, Any] = {"ator": actor, "jeito": way}
            if fields is not None and FIELD[way] not in fields:
                row["erro"] = f"ator não tem {FIELD[way]} (pulado)"
                results.append(row)
                print(f"  {way:<10} {row['erro']}")
                continue
            t0 = time.time()
            try:
                info, items = run(actor, token, payload(way, handles, day, args.max))
                row.update(status=info.get("status"), msg=(info.get("statusMessage") or "")[:120],
                           seg=round(time.time() - t0), usd=round(info.get("usageTotalUsd") or 0, 4),
                           **summarize(items, handles, midnight))
            except Exception as exc:  # noqa: BLE001
                row["erro"] = str(exc)[:200]
            results.append(row)
            print(f"  {way:<10} " + (row.get("erro") or
                  f"{row['status']} {row['seg']}s US${row['usd']}  itens={row['itens']} da_conta={row['da_conta']} "
                  f"hoje={row['hoje']} contas={row['contas_achadas']}/{len(handles)} views={row['views']} "
                  f"tem_views={row['tem_views']}" + (f"  faltaram={row['faltaram']}" if row['faltaram'] else "")
                  + (f"  msg={row['msg']}" if row['msg'] else "")))
        print()

    best = max((r for r in results if "erro" not in r),
               key=lambda r: (r["contas_achadas"], r["hoje"], bool(r["tem_views"]), -r["usd"]), default=None)
    if best:
        print(f"Melhor: {best['ator']} / {best['jeito']} — {best['contas_achadas']}/{len(handles)} contas, "
              f"{best['hoje']} posts de hoje, views {'sim' if best['tem_views'] else 'NÃO'}, US${best['usd']}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
