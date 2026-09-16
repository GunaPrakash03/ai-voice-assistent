# Deploying the Voice Agent Service on Railway (single project)

This guide takes the stack in `docker-compose.yml` and puts it into **one Railway project** with
one service per box below. Everything except the media plane runs on Railway.

```
┌──────────────────────── Railway project: voice-agent ────────────────────────┐
│                                                                              │
│  ┌─────────────┐   ┌────────────────┐   ┌────────────────┐                   │
│  │  Postgres   │◄──┤  dashboard     │   │  agent-worker  │                   │
│  │  (managed)  │◄──┤  scripts/      │   │  agent/worker  │                   │
│  │             │   │  serve.py      │   │  .py  start    │                   │
│  └─────────────┘   └───────┬────────┘   └───────┬────────┘                   │
│                            │ public domain      │ outbound WSS only          │
└────────────────────────────┼────────────────────┼────────────────────────────┘
                             │                    │
                    browser / softphone      wss://<project>.livekit.cloud
                                                  │
                                       ┌──────────▼──────────┐
                                       │  LiveKit Cloud      │  ← media (WebRTC/UDP),
                                       │  rooms + SIP        │    SIP trunks, TURN
                                       └─────────────────────┘
```

## 1. Why LiveKit itself does not go on Railway

Locally `docker-compose.yml` runs `livekit/livekit-server` with `50000-50100/udp` open for RTP.
Railway only exposes **HTTP and TCP** ingress — there is no UDP port publishing and no way to
advertise a public IP in ICE candidates. That breaks two things we depend on:

| Need | Local | On Railway |
|---|---|---|
| WebRTC media (browser ↔ room) | UDP 50000-50100, TCP 7881 fallback | No UDP; TCP proxy gives a random port that LiveKit cannot advertise → ICE fails |
| SIP / PSTN (task 2.1, 2.2, softphone) | LiveKit SIP service, RTP over UDP | Impossible |
| TURN for callers behind NAT | README "Before this goes to a cloud VM" #4 | Not available |

So the media plane moves to **LiveKit Cloud** (free tier is enough for testing; SIP is built in,
no separate `livekit-sip` container). Redis is only there to serve `livekit-server`
(`config/livekit.yaml` → `redis.address`); nothing in `agent/` or `scripts/serve.py` uses it,
so it is **not** deployed either.

> Alternative if you must self-host LiveKit: put `livekit-server` (+ Redis) on a plain VM
> (Hetzner / EC2 / Lightsail) with `rtc.use_external_ip: true`, TLS on 7880 and the UDP range open.
> The Railway side of this guide stays exactly the same — only `LIVEKIT_URL` changes.

## 2. Code changes needed before the first deploy

Three small changes. Commit them; Railway builds from the repo.

### 2.1 `scripts/serve.py` — read secrets from the environment, not only `.env`

`.env` is gitignored and does not exist in the container, and `env()` calls `SystemExit` when a
key is missing. Make it fall back to `os.environ`:

```python
def env(name):
    val = os.getenv(name)
    if val:
        return val
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(f"{name} missing from environment and .env")
```

Also let Railway's `PORT` win over the default `8091`:

```python
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.getenv("PORT", "8091"))
```

### 2.2 `Dockerfile` — production entrypoint for the worker

`dev` hot-reloads and is meant for the compose volume mount. Use `start` (the Silero VAD model
ships inside the `livekit-plugins-silero` wheel, so nothing needs downloading at build time):

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent/ ./agent/
COPY config/ ./config/
COPY samples/ ./samples/
CMD ["python", "-m", "agent.worker", "start"]
```

(`config/` and `samples/` are copied because the worker reads `config/agents.json` and
`/app/samples/speech.wav`; at boot `storage.bootstrap()` overwrites `config/*.json` and
`recordings/` with whatever is in Postgres, so the copied files are only a cold-start fallback.)

### 2.3 `Dockerfile.dashboard` — new file for the web dashboard

`scripts/serve.py` needs the whole repo (`web/`, `config/`, `agent/`, `samples/`):

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p recordings
CMD ["sh", "-c", "python scripts/serve.py ${PORT:-8091}"]
```

Add a `.dockerignore` so `.env`, `scratch/`, `recordings/` and `audio/` never land in an image:

```
.env
.git
scratch/
recordings/
audio/
__pycache__/
*.pyc
```

## 3. LiveKit Cloud setup (once)

1. https://cloud.livekit.io → create project → **Settings → Keys** → note
   `LIVEKIT_URL` (`wss://<name>.livekit.cloud`), `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`.
2. **SIP → Trunks**: recreate the Twilio/Telnyx inbound + outbound trunks that
   `config/sip_trunks.json` describes (the dashboard's Telephony page does this through the
   LiveKit SIP API once the keys above are set, so this can also be done after deploy).
3. Point the carrier's SIP URI at the LiveKit Cloud SIP endpoint shown on that page
   (replaces the local `livekit-sip` target).

## 4. Create the Railway project

All commands run from the repo root. The CLI (`railway 5.41.0` is installed here) will open a
browser for sign-in the first time.

```bash
# 4.1 Project + Postgres
railway init --name voice-agent-service
railway add --database postgres --json

# 4.2 Two empty services, both built from this repo
railway add --service agent-worker --json
railway add --service dashboard --json
```

Then in the dashboard (or `railway service` → Settings) for each service:

| Service | Source | Builder | Dockerfile path | Public domain |
|---|---|---|---|---|
| `agent-worker` | this GitHub repo, branch `main` | Dockerfile | `Dockerfile` | **none** (outbound only) |
| `dashboard` | this GitHub repo, branch `main` | Dockerfile | `Dockerfile.dashboard` | yes → `railway domain --service dashboard` |

Connecting the GitHub repo makes every push to `main` redeploy both services. If you would rather
push from the laptop, `railway up --service agent-worker` / `railway up --service dashboard`
does the same thing without GitHub.

## 5. Variables

Set on **both** services unless noted. Use the Railway *shared variables* feature for the ones
marked ⟲ so they are defined once.

```bash
# Shared (set once in Project → Settings → Shared Variables, then reference from both services)
LIVEKIT_URL=wss://<name>.livekit.cloud            ⟲
LIVEKIT_API_KEY=<from LiveKit Cloud>               ⟲
LIVEKIT_API_SECRET=<from LiveKit Cloud>            ⟲
DATABASE_URL=${{Postgres.DATABASE_URL}}            ⟲  (Railway reference → private network)

# Voice pipeline (worker needs them; dashboard needs them for the Agent Builder voice previews)
DEEPGRAM_API_KEY=
STT_PROVIDER=deepgram
STT_MODEL=nova-3
OPENAI_API_KEY=
GEMINI_API_KEY=
LLM_MODEL=gpt-4o-mini
CARTESIA_API_KEY=
CARTESIA_VOICE_ID=f786b574-daa5-4673-aa0c-cbe3e8534c02
TTS_MODEL=sonic-3
ELEVEN_API_KEY=

# Telephony (dashboard only — carrier API calls come from serve.py)
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TELNYX_API_KEY=
SIP_OUTBOUND_USER=
SIP_OUTBOUND_PASS=

# Dashboard only
AUTH_JWT_SECRET=<openssl rand -hex 32>   # replaces the hard-coded default in agent/auth_manager.py:36
AUTH_TRUST_LOOPBACK=0                    # never trust "loopback" behind Railway's proxy
```

From the CLI:

```bash
railway variable set LIVEKIT_URL=wss://... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... --service agent-worker
railway variable set LIVEKIT_URL=wss://... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... --service dashboard
railway variable set 'DATABASE_URL=${{Postgres.DATABASE_URL}}' --service agent-worker
railway variable set 'DATABASE_URL=${{Postgres.DATABASE_URL}}' --service dashboard
railway variable set AUTH_JWT_SECRET=$(openssl rand -hex 32) AUTH_TRUST_LOOPBACK=0 --service dashboard
# ...and the provider keys the same way, or paste them in the UI.
```

Notes

- **Do not set `PORT` on the dashboard** — Railway injects it and `serve.py` (after §2.1) reads it.
- The worker has no inbound port; it registers with LiveKit over the outbound WebSocket, so
  Railway's health check must be disabled for it (Settings → Deploy → Healthcheck path: empty).
- Provider keys entered in the dashboard's **API Keys & Providers** page are written to `.env`
  *inside the dashboard container* (`provider_manager.save_keys`). That file disappears on the
  next deploy and the worker never sees it. On Railway, treat the Railway variables as the source
  of truth and set keys there, not in the UI.

## 6. Deploy and verify

```bash
railway up --service dashboard --detach -m "initial railway deploy"
railway up --service agent-worker --detach -m "initial railway deploy"
railway deployment list --service dashboard --json     # wait for SUCCESS
railway deployment list --service agent-worker --json  # wait for SUCCESS
```

Then:

1. `railway logs --service agent-worker --lines 100` → expect
   `registered worker ... url=wss://<name>.livekit.cloud` from the LiveKit agents CLI.
2. `railway logs --service dashboard --lines 100` → expect
   `PostgreSQL storage ready (...): restored N file(s) ...`. If you see
   `PostgreSQL unavailable ... running on files only` instead, `DATABASE_URL` is wrong.
3. Open `https://<dashboard-domain>/login.html`, sign in, go to **Agent Builder**, pick an agent,
   press **Test call**. The browser connects to LiveKit Cloud, the worker joins the room, and the
   agent greets you.
4. `curl -b <session-cookie> https://<dashboard-domain>/api/storage/status` shows the database
   reachable and the `recordings` count.
5. Place a real PSTN call to the DID routed in LiveKit Cloud SIP → **Call History** shows it and
   the recording is stored in Postgres (`recordings` table), so it survives redeploys.

## 7. Persistence

| Data | Where it lives on Railway | Survives redeploy? |
|---|---|---|
| Agents, phone numbers, SIP trunks, users/API keys, post-call jobs | Postgres (`storage.py` mirrors each `config/*.json`) | Yes |
| Call recordings | Postgres `recordings` table (bytes) + restored to `recordings/` on boot | Yes |
| `.env` written by the API Keys page | container filesystem | **No** — use Railway variables |
| `config/auth_store.json` | Postgres via `storage.py` | Yes |

No Railway volume is required. If recordings grow large, move them to a Railway **bucket**
(`railway bucket create recordings`) and swap `storage.store_recording` to S3 — out of scope here.

## 8. Cost / sizing

- `agent-worker`: ~512 MB RAM idle, ~1 GB during a call (Silero VAD + audio buffers). Start with
  1 vCPU / 1 GB; scale replicas horizontally — every replica registers with LiveKit and jobs are
  load-balanced across them.
- `dashboard`: stdlib `ThreadingHTTPServer`, 256 MB is plenty.
- Postgres: hobby plan, recordings are the only thing that grows.
- LiveKit Cloud: billed per participant-minute; SIP minutes on top.

## 9. Rollback

```bash
railway deployment list --service agent-worker --json   # find the previous SUCCESS id
railway redeploy --service agent-worker --deployment <id>
```

## Checklist

- [ ] §2 code changes committed (`serve.py` env fallback + `PORT`, `Dockerfile` `start`, `Dockerfile.dashboard`, `.dockerignore`)
- [ ] LiveKit Cloud project + keys + SIP trunks
- [ ] Railway project with Postgres, `agent-worker`, `dashboard`
- [ ] Shared variables set, `DATABASE_URL` referenced from Postgres
- [ ] `AUTH_JWT_SECRET` rotated, `AUTH_TRUST_LOOPBACK=0`
- [ ] Worker health check disabled, dashboard domain generated
- [ ] Test call from the dashboard succeeds, PSTN call lands in Call History
