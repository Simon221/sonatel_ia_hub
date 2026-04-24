# ── Image de base ─────────────────────────────────────────────
FROM python:3.11-slim

# ── Répertoire de travail ──────────────────────────────────────
WORKDIR /app

# ── Installation des dépendances (couche cachée séparément) ───
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copie du code source ───────────────────────────────────────
COPY . .

# ── Port exposé ───────────────────────────────────────────────
EXPOSE 8081

# ── Démarrage ─────────────────────────────────────────────────
CMD ["python", "server.py"]
