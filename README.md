# Suzana

Painel local de contas do X. Rode a partir desta pasta.

```bash
python -m pip install -r requirements.txt
copy .env.example .env
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Abra http://127.0.0.1:8000 . O Bearer do app do X vai em `X_BEARER_TOKEN` no `.env`. Sem token o painel abre; Verificar responde 503.

O servidor escuta só em 127.0.0.1. Não há login. A pasta `backend/` é o esqueleto anterior — não use ela.
