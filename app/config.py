"""Constantes e caminhos da aplicação."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "templates" / "index.html"
X_API = "https://api.x.com/2"
LOCAL_EMAIL = "local@suzana"
# Único admin: aprova quem entra e controla o robô. ADMIN_EMAIL no .env troca.
DEFAULT_ADMIN_EMAIL = "henrfs@proton.me"

STATUS_LABELS = {
    "healthy": "Saudável",
    "search_ban": "Search Ban",
    "ghost_ban": "Ghost Ban",
    "suspended": "Suspensa",
    "not_found": "Não encontrada",
    "protected": "Protegida",
    "rate_limited": "Limite da API",
    "api_error": "Erro da API",
    "unknown": "Indefinido",
}
WEEKDAYS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
