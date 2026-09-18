# Adding a Phone Number (DID)

How to take a new phone number from "just bought in Twilio" to "a voice agent answers it".

---

## 1. Direct Twilio Media Stream Flow (Default & Recommended - Retell AI Style)

No LiveKit Cloud, no SIP trunks, no dispatch rules, no middleman:

```
Caller ──PSTN──▶ Twilio number ──webhook──▶ Dashboard (/api/telephony/voice/inbound)
                                                  │  returns TwiML <Connect><Stream>
                                                  ▼
                                 Direct WebSocket (wss://.../api/telephony/media-stream)
                                                  │  native 8kHz mu-law audio packets
                                                  ▼
                                 Direct Agent Pipeline (Deepgram STT ↔ LLM ↔ Aura/Cartesia TTS)
```

### Quick Setup:
1. **Buy number in Twilio** (Voice capability enabled).
2. **Point Voice Webhook** to:
   `https://<your-dashboard-domain>/api/telephony/voice/inbound` (HTTP POST).
3. **Assign Agent** in Dashboard -> **Active Phone Numbers**.
4. **Done!**

---

## 2. Legacy LiveKit SIP Flow (Optional fallback if `TWILIO_CONNECTION_MODE=sip`)

```
Caller ──PSTN──▶ Twilio number ──webhook──▶ Dashboard (/api/telephony/voice/inbound)
                                                  │  returns TwiML <Dial><Sip>
                                                  ▼
                               LiveKit Cloud SIP  3k0byilfyuy.sip.livekit.cloud
                                                  │  inbound trunk ST_r57mNvdrr25e
                                                  │  dispatch rule  SDR_PntnXKUQDxQn → room "call-…"
                                                  ▼
                                            Worker (agent) joins room
                                            looks up DID → agent mapping in the dashboard DB
```

---

## Reference values

Fixed IDs for this project. You should not need to change any of them when adding a number.

| Thing | Value |
| --- | --- |
| LiveKit project | `voice Agent` — `p_3k0byilfyuy` |
| **LiveKit SIP URI** | `3k0byilfyuy.sip.livekit.cloud` |
| LiveKit WebRTC URL | `wss://voice-agent-kuxp4ocs.livekit.cloud` |
| LiveKit inbound trunk | `ST_r57mNvdrr25e` (`open-carrier-and-softphone-trunk`) |
| LiveKit dispatch rule | `SDR_PntnXKUQDxQn` (individual rooms, prefix `call-`) |
| LiveKit outbound trunk | `ST_QdxBQbiezrWA` (`twilio-voice-agent-outbound`) |
| Twilio Elastic SIP trunk | `TKe7a4a60bd624f62378bd5c1a572bce4d` — `voice-agent-yg.pstn.twilio.com` |
| Twilio credential list | `CLb0079f83436c49ca75b9e92cc74239d7` (`voice agent`) |
| Dashboard | `https://dashboard-production-55c4.up.railway.app` |

> **The SIP URI is not the WebRTC URL.** The SIP host is the project id (`p_3k0byilfyuy`)
> with the `p_` prefix removed. `voice-agent-kuxp4ocs.sip.livekit.cloud` looks right but is a
> shared ingress that holds none of our trunks — every call gets `404 No trunk found`.

Secrets live in `.env` locally and in the Railway service variables. Never put them in this file.

| Variable | Used for |
| --- | --- |
| `LIVEKIT_SIP_DOMAIN` | the SIP URI above |
| `LIVEKIT_SIP_USERNAME` / `LIVEKIT_SIP_PASSWORD` | digest auth on trunk `ST_r57mNvdrr25e` |
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | LiveKit management API (scripts below) |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Twilio REST API |

All scripts below assume you are in the repo root with a populated `.env`:

```bash
cd /home/sys0041/project/voice-agent-service
export NEW_DID="+1XXXXXXXXXX"      # the number you are adding, E.164
```

---

## Step 1 — Buy the number in Twilio

**Where:** Twilio Console → Phone Numbers → Manage → Buy a number.

**Do:** pick a number with the **Voice** capability.

**Note:** the account is on a **Trial** plan. Inbound calls work; outbound only reaches verified
numbers. Upgrade before going live.

## Step 2 — Point the number at the dashboard

**Where:** Twilio Console → the number → Voice Configuration → *A call comes in*.

**Do:**

| Field | Value |
| --- | --- |
| Configure with | Webhook |
| URL | `https://dashboard-production-55c4.up.railway.app/api/telephony/voice/inbound` |
| HTTP method | `POST` |
| Elastic SIP Trunk | leave **unset** — we bridge via TwiML, not trunk origination |

The dashboard (`scripts/serve.py`) answers every call with TwiML that dials LiveKit over SIP,
carrying the trunk credentials:

```xml
<Response><Dial answerOnBridge="true" timeout="30">
  <Sip username="…" password="…">sip:+1XXXXXXXXXX@3k0byilfyuy.sip.livekit.cloud</Sip>
</Dial></Response>
```

**Verify** (no call needed):

```bash
curl -s -X POST "https://dashboard-production-55c4.up.railway.app/api/telephony/voice/inbound" \
     -d "To=$NEW_DID&From=%2B15551112222"
```

The `<Sip>` element must contain your new number and the host `3k0byilfyuy.sip.livekit.cloud`.
If `username=`/`password=` are missing, the dashboard was deployed without
`LIVEKIT_SIP_USERNAME`/`PASSWORD` — fix the Railway variables and redeploy.

## Step 3 — Add the number to the LiveKit inbound trunk

**This is the step that actually makes the call route, and the one that gets forgotten.**
LiveKit only accepts an INVITE whose `To` number is on a trunk's `numbers` list; anything else
gets `404 No trunk found`.

**Do:**

```bash
python3 - <<'EOF'
import asyncio, os
from dotenv import load_dotenv; load_dotenv(".env")
from livekit import api

TRUNK = "ST_r57mNvdrr25e"
NEW   = os.environ["NEW_DID"]

async def main():
    lk = api.LiveKitAPI(os.getenv("LIVEKIT_URL"), os.getenv("LIVEKIT_API_KEY"),
                        os.getenv("LIVEKIT_API_SECRET"))
    try:
        cur = next(t for t in (await lk.sip.list_inbound_trunk(
            api.ListSIPInboundTrunkRequest())).items if t.sip_trunk_id == TRUNK)
        nums = list(cur.numbers)
        # keep the bare and national forms too; carriers are inconsistent about format
        for v in (NEW, NEW.lstrip("+"), NEW.lstrip("+")[1:]):
            if v not in nums:
                nums.append(v)
        r = await lk.sip.update_inbound_trunk_fields(TRUNK, numbers=nums)
        print("numbers now:", list(r.numbers))
    finally:
        await lk.aclose()

asyncio.run(main())
EOF
```

**Verify:** the printed `numbers now:` list contains the new DID.

No new dispatch rule is needed — `SDR_PntnXKUQDxQn` is trunk-scoped and covers every number
on the trunk.

## Step 4 — Register the number in the dashboard and assign an agent

This step does **not** touch LiveKit. It creates the DID → agent mapping the worker reads on
every call (`DID +1… is assigned to agent '…'` in the logs). Without it the call still connects,
but the default agent answers.

**Where (UI):** Dashboard → Call Desk → **SIP Trunks & DIDs** → add the number to an inbound
trunk (or create one). The number then appears under **Active Phone Numbers**; click **Edit** →
*Assign to Voice Agent* → pick the agent.

**Where (API):** (sign in first — the endpoints require a dashboard session cookie)

```bash
DASH="https://dashboard-production-55c4.up.railway.app"

# 1. register the DID (creates the trunk record + owned-number record)
curl -s -b cookies.txt -X POST "$DASH/api/telephony/trunks/inbound" -H 'Content-Type: application/json' \
  -d "{\"trunk_id\":\"trunk-in-newdid\",\"name\":\"New DID\",\"numbers\":[\"$NEW_DID\"],
       \"allowed_addresses\":[\"0.0.0.0/0\"]}"

# 2. assign it to an agent
curl -s -b cookies.txt -X POST "$DASH/api/telephony/numbers/update" -H 'Content-Type: application/json' \
  -d "{\"phone_number\":\"$NEW_DID\",\"agent_name\":\"<Agent Name>\"}"
```

Field rules for `/api/telephony/trunks/inbound` (`agent/telephony_manager.py`,
`validate_inbound_trunk_payload`):

| Field | Rule |
| --- | --- |
| `trunk_id` | 3–64 chars, `A-Z a-z 0-9 - _ .`; must be unique (pass `"replace": true` to overwrite) |
| `name` | required, non-empty |
| `numbers` | list of E.164 strings |
| `allowed_addresses` | IP or CIDR, defaults to `0.0.0.0/0` |
| `auth_username` / `auth_password` | optional |

**Verify:** the DID shows in **Active Phone Numbers** with the right agent in the
*Assigned Voice Agent* column.

## Step 5 — Outbound caller ID (only if the agent dials out from this number)

Skip this if the number is inbound-only.

**Do:**

```bash
python3 - <<'EOF'
import asyncio, os
from dotenv import load_dotenv; load_dotenv(".env")
from livekit import api
async def main():
    lk = api.LiveKitAPI(os.getenv("LIVEKIT_URL"), os.getenv("LIVEKIT_API_KEY"),
                        os.getenv("LIVEKIT_API_SECRET"))
    try:
        r = await lk.sip.update_outbound_trunk_fields(
            "ST_QdxBQbiezrWA", numbers=[os.environ["NEW_DID"]])
        print("caller IDs:", list(r.numbers))
    finally:
        await lk.aclose()
asyncio.run(main())
EOF
```

**Also required:** the dashboard's outbound trunk record must carry
`metadata.livekit_trunk_id = "ST_QdxBQbiezrWA"`. Without it `dial_phone_number` sends our
local trunk id to LiveKit, which rejects it with the same `404`.

## Step 6 — Verify

### 6a. SIP probe (no phone, no carrier charges)

```bash
python3 scripts/sip_probe.py $NEW_DID          # routing only
python3 scripts/sip_probe.py $NEW_DID --rtp    # routing + 8 s of media
```

A healthy number:

```
--> INVITE sip:+1XXXXXXXXXX@3k0byilfyuy.sip.livekit.cloud
   <-- SIP/2.0 100 Processing
   <-- SIP/2.0 407 Unauthorized
--> INVITE (authenticated)
   <-- SIP/2.0 180 Ringing
   <-- SIP/2.0 200 OK

RESULT: accepted — the DID routes and an agent was dispatched
    inbound RTP packets: 391  ->  media path OK
```

On failure the probe prints a one-line diagnosis that maps to the troubleshooting table below.
The probe dispatches a real agent job, so a `joined room call-_…` line will appear in the worker
logs.

### 6b. Real call

Call the number and watch the Railway **worker** logs for, in order:

```
joined room call-_…
DID +1XXXXXXXXXX is assigned to agent '…'
Agent state changed: listening -> speaking
Call … filed in Call History (N turns, audio=True)
```

If you are testing Railway, make sure the local worker is stopped first
(`docker stop voice-agent-worker`) — both are registered to the same LiveKit project and the
local one may win the job.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `404 No trunk found` | Number missing from `ST_r57mNvdrr25e.numbers` | Step 3 |
| `404 No trunk found` on **every** number, never a `407` | Wrong SIP host — WebRTC URL used instead of SIP URI | Use `3k0byilfyuy.sip.livekit.cloud` |
| `407 Unauthorized`, call never connects | Credentials don't match the trunk | Align `LIVEKIT_SIP_USERNAME/PASSWORD` with the trunk, redeploy |
| Twilio call fails but softphone (Zoiper) works | Dashboard TwiML omits credentials | Set the vars on Railway, redeploy `scripts/serve.py` |
| Wrong agent answers | DID not registered / not assigned | Step 4 |
| Call connects, **silence both ways** | Softphone not using symmetric RTP | Zoiper → Account → Advanced → **Use rport media** on; **Use STUN** off |
| Agent speaks but never hears you | Same as above, or mic muted | Check softphone stats: sent vs received packets |
| Greeting is old text after editing the prompt | Greeting comes from `first_message` (spoken verbatim); the prompt's Opening is unused when `speak_first: "ai"` | Set **First Message** equal to the prompt's Opening |
| Edits have no effect; recordings land on the laptop | Local `voice-agent-worker` container won the job | `docker stop voice-agent-worker` |
| Recordings vanish after a redeploy | Railway filesystem is ephemeral | Attach a volume at `/app/recordings`; the Postgres `recordings` table copy survives regardless |

### Inspect live state

```bash
# LiveKit trunks and dispatch rules
python3 - <<'EOF'
import asyncio, os
from dotenv import load_dotenv; load_dotenv(".env")
from livekit import api
async def main():
    lk = api.LiveKitAPI(os.getenv("LIVEKIT_URL"), os.getenv("LIVEKIT_API_KEY"),
                        os.getenv("LIVEKIT_API_SECRET"))
    try:
        print(await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest()))
        print(await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest()))
        print(await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest()))
    finally:
        await lk.aclose()
asyncio.run(main())
EOF
```

```bash
# Twilio numbers and their voice webhooks
python3 - <<'EOF'
import os, requests
from dotenv import load_dotenv; load_dotenv(".env")
sid = os.getenv("TWILIO_ACCOUNT_SID")
auth = (sid, os.getenv("TWILIO_AUTH_TOKEN"))
r = requests.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json",
                 auth=auth).json()
for n in r.get("incoming_phone_numbers", []):
    print(n["phone_number"], "->", n.get("voice_url"))
EOF
```

---

## Removing a number

Reverse the steps: remove it from the LiveKit inbound trunk `numbers` list (Step 3 script with
the number filtered out), delete it in the dashboard (Active Phone Numbers → Delete, or
`POST /api/telephony/numbers/delete` with `{"phone_number": "+1…"}`), and release it in Twilio.
