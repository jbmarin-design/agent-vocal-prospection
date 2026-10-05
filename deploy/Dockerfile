# syntax=docker/dockerfile:1.7
# Images de l'agent vocal (contexte de build : racine du dépôt).
#
#   docker compose build                     (depuis deploy/)
#   docker build -f deploy/Dockerfile --target agent-worker -t avp/agent-worker:local .
#   docker build -f deploy/Dockerfile --target orchestrator -t avp/orchestrator:local .
#
# Cibles :
#   agent-worker  : livekit-agents + plugins audio (extra [agent]) + modèles silero / turn-detector
#                   téléchargés AU BUILD (aucun téléchargement au premier appel).
#   orchestrator  : CLI `avp` et démon (livekit-api, httpx, anthropic), sans plugins audio.
#
# Les deux images exécutent le code en mode « editable » depuis /app/src : PROJECT_ROOT vaut /app,
# et campaigns/ prompts/ data/ sont montés par docker-compose.

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1 \
    HOME=/tmp \
    TZ=Europe/Paris
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# Squelette minimal pour installer les dépendances dans une couche mise en cache
# (elle n'est reconstruite que si pyproject.toml change).
COPY pyproject.toml README.md* ./
RUN test -f README.md || printf '# agent-vocal-prospection\n' > README.md \
 && mkdir -p src/avp && printf '' > src/avp/__init__.py

# ---------------------------------------------------------------------------------------------
FROM base AS orchestrator
RUN --mount=type=cache,target=/root/.cache/pip pip install -e .
COPY src ./src
RUN mkdir -p /app/data /app/campaigns /app/prompts && chmod 777 /app/data \
 && python -c "import avp, avp.config; print('avp importable')"
CMD ["avp", "run"]

# ---------------------------------------------------------------------------------------------
FROM base AS agent-worker
# Cache des modèles Hugging Face (turn-detector) lisible par l'utilisateur non root du conteneur.
ENV HF_HOME=/opt/hf-cache
RUN --mount=type=cache,target=/root/.cache/pip pip install -e ".[agent]"
COPY src ./src
# Téléchargement des modèles (silero VAD, turn-detector multilingue) au build.
# Forme demandée par le projet : `python -m avp.agent.worker download-files` ; repli sur la commande
# recommandée par LiveKit Agents >= 1.5.10 (`python -m livekit.agents download-files`).
RUN mkdir -p "$HF_HOME" \
 && ( python -m avp.agent.worker download-files \
      || python -m livekit.agents download-files ) \
 && chmod -R a+rX "$HF_HOME" \
 && mkdir -p /app/data /app/campaigns /app/prompts && chmod 777 /app/data
# À l'exécution, ne pas retenter de téléchargement (les fichiers sont dans l'image).
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1
EXPOSE 8081
CMD ["python", "-m", "avp.agent.worker", "start"]
