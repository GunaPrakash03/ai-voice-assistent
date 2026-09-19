# Voice Agent Platform — Comprehensive User Documentation & Operational Manual

## 1. Executive Summary & Architecture Overview
The **Voice Agent Service** is an enterprise-grade, conversational voice AI platform designed for high-concurrency telephony and WebRTC communications. The system provides:
- **Sub-second Speech-to-Speech Latency:** Real-time conversational responsiveness using streaming STT (Deepgram Nova-3), intelligent LLM reasoning (Gemini / OpenAI), and ultra-fast neural synthesis (Cartesia / Deepgram Aura).
- **Multi-Tenant Workspace Security:** Strict role-based isolation between workspaces, users, and external integrations.
- **Retell AI Webhook Compatibility:** Natively emits Retell AI event schemas (`call_started`, `call_ended`, `call_analyzed`) to power automations in Make, n8n, and custom CRMs.
- **Standalone Webhook Receiver:** An independent service (port 8095) with SQLite storage and a live inspector dashboard.

---

## 2. Role-Based Access Control (RBAC): User vs. Product Admin

| Feature / Page | User Role | Product Admin Role | Notes |
| :--- | :---: | :---: | :--- |
| **Overview & Metrics** (`/overview`) | ✅ | ✅ | Real-time call volume, duration, and sentiment KPIs. |
| **Call Desk & Audio Player** (`/call-desk/calls`) | ✅ | ✅ | Waveform player, turn-by-turn transcripts, and CRM data. |
| **WebRTC Softphone** (`/softphone.html`) | ✅ | ✅ | In-browser dialer with DTMF keypad, mute, and transfer. |
| **User Profile & Sessions** (`/profile`) | ✅ | ✅ | Personal settings, active sessions, and theme toggles. |
| **Agent Prompt Builder** (`/agent-builder`) | ❌ | ✅ | Gated behind HTTP 403 / redirect to `/?denied=admin`. |
| **Telephony & SIP Trunks** (`/telephony`) | ❌ | ✅ | Add/manage owned phone numbers and SIP trunks. |
| **API Keys & Scopes** (`/api-keys`) | ❌ | ✅ | Mint scoped tokens (`calls:dispatch`, `webhooks:admin`). |
| **Webhook Management Hub** (`/webhooks`) | ❌ | ✅ | Configure endpoints, inspect DLQ, and test events. |

---

## 3. Screen-by-Screen Operational Walkthrough

### 3.1 Authentication & Login
- Passwordless OTP login via SMS or Email.
- Mints an encrypted `va_session` cookie valid for 7 days (or 30 days if "Remember me" is checked).
- Screenshot: `scratch/user_screenshots/01_login_screen.png`

### 3.2 Workspace Overview
- Aggregated workspace metrics: Total Calls, Total Duration, Inbound vs Outbound, Average Duration, and Sentiment Breakdown.
- Screenshot: `scratch/user_screenshots/02_overview_dashboard.png`

### 3.3 Call Desk & Audio Review
- Searchable call history with filtering by direction, date, and status.
- Waveform audio player with scrubbing and playback speed controls.
- Full speaker-attributed conversation transcripts.
- AI-extracted CRM entities (Name, Callback Number, Matter Type, Urgency).
- Screenshot: `scratch/user_screenshots/03_call_desk_view.png`

### 3.4 In-Browser WebRTC Softphone
- Dial any PSTN number directly from Chrome/Firefox without hardware.
- Full DTMF touch-tone pad (0–9, *, #) for IVR navigation.
- Call controls: Mute, Hold, and Warm Transfer to support representatives.
- Screenshot: `scratch/user_screenshots/05_softphone_dialer.png`

### 3.5 Webhook Management Hub (`/webhooks`)
- **KPI Bar:** Total Deliveries, Success Rate %, Dead-Letter Queue (DLQ) count, Average Latency (ms), and Active Endpoints.
- **Endpoints Table:** Register target URLs, select events (`call_started`, `call_ended`, `call_analyzed`), and rotate HMAC secrets.
- **Interactive Simulator:** Dispatch synthetic payloads to any endpoint with live latency and response status reporting.
- **HMAC Sandbox:** Test signature verification algorithms with sample code for Node.js, Python, and Go.
- Screenshot: `scratch/user_screenshots/07_webhook_dashboard.png`

### 3.6 Dedicated Webhook Collector & Storage Site (Port 8095)
- Independent receiver service running at `http://localhost:8095`.
- Ingestion endpoint: `POST http://localhost:8095/webhook`.
- Saves every event into `webhook_receiver/webhooks.db`.
- Deep Inspector Modal: Click "Inspect" to view the full Retell AI payload, AI call summary, extracted CRM fields, transcript, and headers.
- Screenshots: `scratch/user_screenshots/10_receiver_dashboard.png` & `11_receiver_inspector_modal.png`

---

## 4. Key Local Service URLs
- **Main Voice Agent Platform:** http://localhost:8091 (or 8080)
- **Call Desk:** http://localhost:8091/call-desk/calls
- **WebRTC Softphone:** http://localhost:8091/softphone.html
- **Webhooks Hub:** http://localhost:8091/webhooks
- **Webhook Collector & Receiver:** http://localhost:8095
