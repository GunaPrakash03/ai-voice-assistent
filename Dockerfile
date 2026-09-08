# The agent worker. Runs the same way here and on a cloud VM.
FROM python:3.12-slim

WORKDIR /app

# Pinned in requirements.txt — the Agents SDK changed breakingly 0.x -> 1.x.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./agent/

# "dev" reloads on change; "start" is the production entrypoint.
CMD ["python", "-m", "agent.worker", "dev"]
