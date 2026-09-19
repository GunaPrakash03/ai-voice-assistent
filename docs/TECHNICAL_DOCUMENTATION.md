# Voice Agent Platform — Complete Technical Architecture & Engineering Manual

## 1. System Architecture & Audio Topology

```
                  +----------------------------------------------+
                  |               Carriers / PSTN                |
                  |     (Twilio Programmable Voice / SIP Trunks)  |
                  +----------------------+-----------------------+
                                         |
                       RFC 6455 Media Stream (8kHz mu-law)
                                         |
                                         v
                  +----------------------------------------------+
                  |           TwilioMediaStreamSession           |
                  |     (Direct WebSocket Frame Handler)         |
                  +----------------------+-----------------------+
                                         |
                     +-------------------+-------------------+
                     |                                       |
                     v                                       v
        +-------------------------+             +-------------------------+
        |  Deepgram Nova-3 STT    |             |   LLM Inference Engine  |
        |  Streaming WebSocket    |             |   (Gemini 2.5 Flash /   |
        |  (100 - 180 ms latency) |             |    OpenAI GPT-4o-mini)  |
        +------------+------------+             +------------+------------+
                     |                                       |
                     +-------------------+-------------------+
                                         |
                                         v
                  +----------------------------------------------+
                  |       Neural TTS Synthesis Engine            |
                  |    (Deepgram Aura / Cartesia Sonic mu-law)   |
                  +----------------------+-----------------------+
                                         |
                                         v
                  +----------------------------------------------+
                  |         Post-Call Pipeline Worker            |
                  |  (Normalization -> Sentiment -> Extraction)  |
                  +----------------------+-----------------------+
                                         |
                                         v
                  +----------------------------------------------+
                  |      HMAC-SHA256 Webhook Dispatcher          |
                  |   (Retell AI Schema: call_started, ended,    |
                  |               call_analyzed)                 |
                  +----------------------+-----------------------+
                                         |
                                         v
                  +----------------------------------------------+
                  |   Standalone SQLite Receiver Site (:8095)    |
                  +----------------------------------------------+
```

---

## 2. Audio Pipeline Specifications

| Pipeline Stage | Implementation | Data Format | Target Latency |
| :--- | :--- | :--- | :--- |
| **Ingestion** | `agent/twilio_stream.py` | 8000 Hz 8-bit G.711 mu-law, 20ms chunks (160 bytes) | &lt; 20 ms |
| **STT Engine** | Deepgram Nova-3 WebSocket | Streaming raw mu-law bytes, `endpointing=350ms` | 100 - 180 ms |
| **Barge-In Handler** | Automatic speech energy check | Halts TTS playback and emits Twilio `clear` event | &lt; 50 ms |
| **LLM Engine** | Gemini 2.5 Flash / GPT-4o-mini | Streaming tokens with conversational prompt rules | 180 - 320 ms |
| **TTS Engine** | Deepgram Aura / Cartesia Sonic | Native 8kHz mu-law output without transcoding | 90 - 150 ms |
| **Post-Call Pipeline** | `agent/pipeline_worker.py` | Asynchronous 6-stage background worker queue | 20 - 35 ms |

---

## 3. Retell AI Webhook Specification

The platform emits requests complying with the Retell AI schema:

### Events:
1. `call_started`: Dispatched immediately upon stream connection.
2. `call_ended`: Dispatched upon call disconnect (includes duration and formatted transcript).
3. `call_analyzed`: Dispatched after Gemini AI analysis (includes `call_analysis`, `call_summary`, `user_sentiment`, and `custom_analysis_data`).

### Headers:
```http
POST /webhook HTTP/1.1
Host: api.yourcompany.com
Content-Type: application/json
X-Retell-Signature: sha256=02fd24a3c413aa089b3b22ac4235b9f80a9aa8222cbdd9ad79f6d232e4de0326
X-Signature-256: sha256=02fd24a3c413aa089b3b22ac4235b9f80a9aa8222cbdd9ad79f6d232e4de0326
X-Webhook-Timestamp: 1726732943
X-Webhook-Delivery: whd_d0ba0cc0861c4506
X-Webhook-Event: call_analyzed
```

### Complete Payload Example:
```json
{
  "event": "call_analyzed",
  "call": {
    "call_id": "call_01j7abc9921",
    "call_type": "phone_call",
    "agent_id": "agent_maya_law",
    "call_status": "ended",
    "start_timestamp": 1726732800000,
    "end_timestamp": 1726732942000,
    "duration_ms": 142000,
    "transcript": "Agent: Bottini Legal, how can I help?\nCaller: I have a partnership dispute.",
    "transcript_object": [
      { "role": "agent", "content": "Bottini Legal, how can I help?", "words": [] },
      { "role": "user", "content": "I have a partnership dispute.", "words": [] }
    ],
    "recording_url": "https://api.yourdomain.com/recordings/call_9921.mp3",
    "disconnection_reason": "user_hangup",
    "from_number": "+19515550192",
    "to_number": "+18005550199",
    "direction": "inbound",
    "call_analysis": {
      "call_summary": "Caller requested consultation regarding partnership dispute.",
      "user_sentiment": "Positive",
      "call_successful": true,
      "custom_analysis_data": {
        "caller_name": "Alexander Wright",
        "matter_type": "Partnership Dispute",
        "urgency": "High"
      }
    }
  }
}
```

---

## 4. Performance & Multi-Tenancy Benchmarks

| Metric | Measured Result | Capacity | Verification |
| :--- | :--- | :--- | :--- |
| **Authenticated Call Desk Query** | **4.12 ms avg** | &gt; 240 QPS/core | In-memory token cache with DB write-through |
| **Cross-Tenant Violation Rejection** | **0.41 ms** | &gt; 2,400 QPS | Immediate HTTP 403 zero-leakage |
| **Session Token Minting** | **57.90 ms** | &gt; 17 logins/sec | PBKDF2-HMAC-SHA256 password hash |
| **Webhook Ingestion & Storage** | **1.40 ms** | &gt; 700 deliveries/sec | SQLite persistent commit with WAL mode |
