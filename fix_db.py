"""Script one-time para adicionar a coluna is_archived no PostgreSQL do Render.

Uso recomendado:
- Rode via terminal do Render (One-off command) ou
- Acesse via rota /fix-db no navegador (ver main.py)

Não rode localmente se estiver usando SQLite.
"""

import os
from sqlalchemy import create_engine, text

# Reutiliza a mesma lógica do main.py
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url
    return "sqlite:///" + os.path.join(os.path.dirname(__file__), "data", "suzana.db")

def main():
    url = database_url()
    print(f"Conectando em: {url.split('@')[-1] if '@' in url else url}")

    engine = create_engine(url)

    with engine.connect() as conn:
        conn.execute(text("""
            ALTER TABLE x_accounts 
            ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE;
        """))
        conn.commit()

    print("Coluna adicionada com sucesso!")

if __name__ == "__main__":
    main()
