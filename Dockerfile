# The agent worker. Runs the same way here and on a cloud VM.
FROM python:3.12-slim

WORKDIR /app

# Pinned in requirements.txt — the Agents SDK changed breakingly 0.x -> 1.x.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./agent/
# Cold-start fallbacks only: storage.bootstrap() restores config/*.json from PostgreSQL at boot,
# and samples/speech.wav is the canned greeting the worker plays when TTS is unavailable.
COPY config/ ./config/
COPY samples/ ./samples/

# "start" is the production entrypoint; docker-compose overrides this with "dev" for hot reload.
CMD ["python", "-m", "agent.worker", "start"]
