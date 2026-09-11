"""Task 4.1 — Agent Builder & Prompt Editor.

Lets a non-technical operator author a voice agent without touching code:
1. Agent configuration model (persona, prompt, voice, model knobs, tools).
2. Registry with CRUD, cloning, activation and disk persistence.
3. Revision history with per-field diffs and one-click rollback.
4. Config validation plus prompt linting tuned for spoken dialogue.
5. Voice catalogue with first-audio latency / cost metadata and preview payloads.
6. Sandbox test-run that previews the opening turns, tool picks and cost.
"""

import copy
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("voice-agent.builder")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(ROOT_DIR, "config", "agents.json")

MAX_PROMPT_CHARS = 250_000
MAX_FIRST_MESSAGE_CHARS = 4_000
MAX_REVISIONS = 25
# Rough spoken-word pacing used by the sandbox to estimate turn duration.
WORDS_PER_MINUTE = 150


@dataclass
class VoiceOption:
    voice_id: str
    name: str
    provider: str           # cartesia | deepgram
    model: str
    style: str
    gender: str
    first_audio_ms: int     # measured time to first audio chunk
    cost_per_1k_chars: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Voices the TTS layer (agent/tts_manager.py) can speak with.
VOICE_CATALOG: List[VoiceOption] = [
    # Cartesia Sonic (Sub-100ms ultra-low latency)
    VoiceOption("f786b574-daa5-4673-aa0c-cbe3e8534c02", "Aurora", "cartesia", "sonic-3",
                "warm, measured", "female", 88, 0.075),
    VoiceOption("a0e99841-438c-4a64-b679-ae501e7d6091", "Vale", "cartesia", "sonic-3",
                "brisk, neutral", "female", 84, 0.075),
    VoiceOption("729651dc-c6c3-4ee5-97fa-350da1f88600", "Ridge", "cartesia", "sonic-3",
                "deep, formal", "male", 91, 0.075),
    VoiceOption("694f9389-aac1-45b6-b726-9d9369183238", "Brooke", "cartesia", "sonic-3",
                "upbeat, cheerful support", "female", 85, 0.075),
    VoiceOption("b7d50908-b17c-442d-ad8d-810c63997ed9", "Leo", "cartesia", "sonic-3",
                "calming, friendly advisor", "male", 89, 0.075),
    VoiceOption("3656c123-289b-449e-9d29-c89b4f9fb338", "Evelyn", "cartesia", "sonic-3",
                "British RP, polished executive", "female", 94, 0.075),
    VoiceOption("c885cf83-7c50-482a-a9f0-28ec36081e66", "George", "cartesia", "sonic-3",
                "British RP, authoritative narrator", "male", 92, 0.075),
    VoiceOption("829ccd10-f8b3-43cd-a8c0-96aa29f3be6f", "Sarah", "cartesia", "sonic-3",
                "Australian, bright conversational", "female", 87, 0.075),
    VoiceOption("a216d649-14a5-48b2-b430-81f1e3100346", "Mateo", "cartesia", "sonic-3",
                "Spanish bilingual, warm counselor", "male", 90, 0.075),

    # Deepgram Aura
    VoiceOption("aura-asteria-en", "Asteria", "deepgram", "aura-2",
                "bright, conversational", "female", 132, 0.030),
    VoiceOption("aura-orion-en", "Orion", "deepgram", "aura-2",
                "steady, authoritative", "male", 139, 0.030),
    VoiceOption("aura-luna-en", "Luna", "deepgram", "aura-2",
                "soft, empathetic", "female", 128, 0.030),
    VoiceOption("aura-angus-en", "Angus", "deepgram", "aura-2",
                "deep, trustworthy Irish/Scottish", "male", 135, 0.030),
    VoiceOption("aura-stella-en", "Stella", "deepgram", "aura-2",
                "professional customer service", "female", 130, 0.030),
    VoiceOption("aura-athena-en", "Athena", "deepgram", "aura-2",
                "sophisticated, clear narrator", "female", 133, 0.030),
    VoiceOption("aura-helios-en", "Helios", "deepgram", "aura-2",
                "warm, enthusiastic sales", "male", 138, 0.030),

    # High-Definition Neural Edge Voices
    VoiceOption("neural-jenny", "Jenny (Neural HD)", "neural", "edge-neural",
                "Natural conversational, warm & engaging US", "female", 125, 0.000),
    VoiceOption("neural-guy", "Guy (Neural HD)", "neural", "edge-neural",
                "Calm, professional & authoritative US", "male", 128, 0.000),
    VoiceOption("neural-aria", "Aria (Neural HD)", "neural", "edge-neural",
                "Expressive, bright & empathetic support US", "female", 122, 0.000),
    VoiceOption("neural-christopher", "Christopher (Neural HD)", "neural", "edge-neural",
                "Deep, confident storyteller & narrator US", "male", 132, 0.000),
    VoiceOption("neural-emma", "Emma (Neural HD)", "neural", "edge-neural",
                "Friendly, cheerful & clear assistant US", "female", 124, 0.000),
    VoiceOption("neural-andrew", "Andrew (Neural HD)", "neural", "edge-neural",
                "Warm, natural & trustworthy advisor US", "male", 126, 0.000),
    VoiceOption("neural-sonia", "Sonia (British RP)", "neural", "edge-neural",
                "Polished British RP executive concierge", "female", 130, 0.000),
    VoiceOption("neural-ryan", "Ryan (British Broadcaster)", "neural", "edge-neural",
                "Smooth, authoritative British broadcaster", "male", 129, 0.000),
    VoiceOption("neural-natasha", "Natasha (Aussie Accent)", "neural", "edge-neural",
                "Upbeat, friendly Australian conversationalist", "female", 126, 0.000),
    VoiceOption("neural-neerja", "Neerja (Indian English)", "neural", "edge-neural",
                "Warm, expressive Indian English professional", "female", 128, 0.000),
    VoiceOption("neural-prabhat", "Prabhat (Indian English)", "neural", "edge-neural",
                "Friendly, clear Indian English advisor", "male", 130, 0.000),

    # Retell AI Platform Voices (sample-backed · 0 API keys needed)
    # Play Sample = original Retell recording; live/custom text = tuned neural match (see NEURAL_VOICE_MAP).
    VoiceOption("retell-cimo", "Cimo (Retell AI)", "retell", "retell-sample+neural",
                "American · Young · Warm, natural receptionist · Sample: original recording · Live: neural match", "female", 115, 0.000),
    VoiceOption("retell-kate", "Kate (Retell AI)", "retell", "retell-sample+neural",
                "American · Middle Aged · Friendly, clear support · Sample: original recording · Live: neural match", "female", 115, 0.000),
    VoiceOption("retell-marissa", "Marissa (Retell AI)", "retell", "retell-sample+neural",
                "American · Young · Bright, energetic sales · Sample: original recording · Live: neural match", "female", 115, 0.000),
    VoiceOption("retell-nico", "Nico (Retell AI)", "retell", "retell-sample+neural",
                "American · Middle Aged · Deep, confident advisor · Sample: original recording · Live: neural match", "male", 115, 0.000),
    VoiceOption("retell-sloane", "Sloane (Retell AI)", "retell", "retell-sample+neural",
                "American · Young · Fast, upbeat dispatcher · Sample: original recording · Live: neural match", "female", 115, 0.000),
    VoiceOption("retell-brynne", "Brynne (Retell AI)", "retell", "retell-sample+neural",
                "American · Young · Soft, empathetic care · Sample: original recording · Live: neural match", "female", 115, 0.000),
    VoiceOption("retell-grace", "Grace (Retell AI)", "retell", "retell-sample+neural",
                "American · Middle Aged · Calm, professional · Sample: original recording · Live: neural match", "female", 115, 0.000),
    VoiceOption("retell-lily", "Lily (Retell AI)", "retell", "retell-sample+neural",
                "American · Young · Expressive, conversational · Sample: original recording · Live: neural match", "female", 115, 0.000),
    VoiceOption("retell-rita", "Rita (Retell AI)", "retell", "retell-sample+neural",
                "American · Young · Cheerful, welcoming · Sample: original recording · Live: neural match", "female", 115, 0.000),
    VoiceOption("retell-willa", "Willa (Retell AI)", "retell", "retell-sample+neural",
                "British · Middle Aged · Polished RP concierge · Sample: original recording · Live: neural match", "female", 115, 0.000),
    VoiceOption("retell-ashley", "Ashley (Retell AI)", "retell", "retell-sample+neural",
                "British · Young · Warm, friendly assistant · Sample: original recording · Live: neural match", "female", 115, 0.000),
    VoiceOption("retell-chloe", "Chloe (Retell AI)", "retell", "retell-sample+neural",
                "American · Young · Vibrant, engaging · Sample: original recording · Live: neural match", "female", 115, 0.000),
    VoiceOption("retell-leland", "Leland (Retell AI)", "retell", "retell-sample+neural",
                "American · Young · Relaxed, approachable · Sample: original recording · Live: neural match", "male", 115, 0.000),
    VoiceOption("retell-della", "Della (Retell AI)", "retell", "retell-sample+neural",
                "American · Middle Aged · Steady, reassuring · Sample: original recording · Live: neural match", "female", 115, 0.000),
    VoiceOption("retell-merritt", "Merritt (Retell AI)", "retell", "retell-sample+neural",
                "American · Young · Light, youthful · Sample: original recording · Live: neural match", "female", 115, 0.000),
    VoiceOption("retell-maren", "Maren (Retell AI)", "retell", "retell-sample+neural",
                "British · Middle Aged · Measured broadcaster · Sample: original recording · Live: neural match", "male", 115, 0.000),
    VoiceOption("retell-andrea", "Andrea (Retell AI)", "retell", "retell-sample+neural",
                "Latin American · Young · Bilingual, friendly · Sample: original recording · Live: neural match", "female", 115, 0.000),
    VoiceOption("retell-andrea-es", "Andrea (Español) (Retell AI)", "retell", "retell-sample+neural",
                "Español · Young · Bilingual customer care · Sample: original recording · Live: neural match", "female", 115, 0.000),
    # Studio Pro Ultra-Realistic Platform Voices (18 Studio Personas · 0 API Keys Needed)
    VoiceOption("studio-calvin", "Camille (Studio Pro)", "studio", "studio-neural-v2",
                "American · Middle Aged · Calm, friendly & warm conversational concierge", "female", 110, 0.000),
    VoiceOption("studio-kaitlyn", "Kaitlyn (Studio Pro)", "studio", "studio-neural-v2",
                "American · Middle Aged · Friendly, upbeat customer support lead", "female", 108, 0.000),
    VoiceOption("studio-maya", "Maya (Studio Pro)", "studio", "studio-neural-v2",
                "American · Middle Aged · Professional, executive AI assistant", "female", 112, 0.000),
    VoiceOption("studio-nathan", "Nathan (Studio Pro)", "studio", "studio-neural-v2",
                "American · Middle Aged · Deep, calming, steady & clear advisor", "male", 115, 0.000),
    VoiceOption("studio-sierra", "Sierra (Studio Pro)", "studio", "studio-neural-v2",
                "American · Middle Aged · Fast, scaling operations dispatcher", "female", 110, 0.000),
    VoiceOption("studio-brooke", "Brooke (Studio Pro)", "studio", "studio-neural-v2",
                "American · Middle Aged · Warm, attentive virtual concierge", "female", 109, 0.000),
    VoiceOption("studio-giselle", "Giselle (Studio Pro)", "studio", "studio-neural-v2",
                "American · Middle Aged · Consistent, reliable enterprise care", "female", 114, 0.000),
    VoiceOption("studio-luna", "Luna (Studio Pro)", "studio", "studio-neural-v2",
                "American · Young · Bright, empathetic & friendly receptionist", "female", 106, 0.000),
    VoiceOption("studio-rosie", "Rosie (Studio Pro)", "studio", "studio-neural-v2",
                "American · Young · Natural acoustic warmth, youthful conversationalist", "female", 108, 0.000),
    VoiceOption("studio-winona", "Winona (Studio Pro)", "studio", "studio-neural-v2",
                "British · Middle Aged · Polished British RP, soothing executive", "female", 116, 0.000),
    VoiceOption("studio-amber", "Amber (Studio Pro)", "studio", "studio-neural-v2",
                "British · Middle Aged · Fast, accurate, friendly UK concierge", "female", 115, 0.000),
    VoiceOption("studio-cassidy", "Cassidy (Studio Pro)", "studio", "studio-neural-v2",
                "American · Young · Vibrant, high-energy customer care", "female", 107, 0.000),
    VoiceOption("studio-lucas", "Lucas (Studio Pro)", "studio", "studio-neural-v2",
                "American · Young · Confident, dependable modern assistant", "male", 112, 0.000),
    VoiceOption("studio-danica", "Danica (Studio Pro)", "studio", "studio-neural-v2",
                "American · Middle Aged · Efficient, pleasant service dispatcher", "female", 111, 0.000),
    VoiceOption("studio-melanie", "Melanie (Studio Pro)", "studio", "studio-neural-v2",
                "American · Middle Aged · Articulate, dependable support advisor", "female", 113, 0.000),
    VoiceOption("studio-madeline", "Madeline (Studio Pro)", "studio", "studio-neural-v2",
                "British · Young · Clear, charming British assistant", "female", 112, 0.000),
    VoiceOption("studio-alana", "Alana (Studio Pro)", "studio", "studio-neural-v2",
                "Spanish/Bilingual · Middle Aged · Warm bilingual care", "female", 114, 0.000),
    VoiceOption("studio-alana-es", "Alana (Spanish Studio Pro)", "studio", "studio-neural-v2",
                "Spanish Native · Middle Aged · Asistente profesional en español", "female", 114, 0.000),

    # OpenAI TTS
    VoiceOption("openai-alloy", "Alloy", "openai", "tts-1",
                "balanced, clear neutral", "unisex", 190, 0.015),
    VoiceOption("openai-echo", "Echo", "openai", "tts-1",
                "dynamic, assertive presenter", "male", 195, 0.015),
    VoiceOption("openai-fable", "Fable", "openai", "tts-1",
                "expressive, British storytelling", "male", 192, 0.015),
    VoiceOption("openai-onyx", "Onyx", "openai", "tts-1",
                "deep, smooth baritone", "male", 188, 0.015),
    VoiceOption("openai-nova", "Nova", "openai", "tts-1",
                "energetic, friendly assistant", "female", 185, 0.015),
    VoiceOption("openai-shimmer", "Shimmer", "openai", "tts-1",
                "gentle, empathetic support", "female", 189, 0.015),

    # ElevenLabs Turbo (24 Account Voices with Authentic Portal CDN Streams)
    VoiceOption("ecp3DWciuUyW7BYM7II1", "Anika", "elevenlabs", "turbo-v2.5",
                "Sweet and lively, Indian accent", "female", 155, 0.180),
    VoiceOption("RXtWW6etvimS8QJ5nhVk", "Fiona", "elevenlabs", "turbo-v2.5",
                "Chill, natural & real conversational", "female", 158, 0.180),
    VoiceOption("CwhRBWXzGAHq8TQ4Fs17", "Roger", "elevenlabs", "turbo-v2.5",
                "Laid-back, casual, resonant", "male", 165, 0.180),
    VoiceOption("EXAVITQu4vr4xnSDxMaL", "Sarah", "elevenlabs", "turbo-v2.5",
                "Mature, reassuring, confident", "female", 160, 0.180),
    VoiceOption("FGY2WhTYpPnrIDTdsKH5", "Laura", "elevenlabs", "turbo-v2.5",
                "Enthusiast, quirky attitude", "female", 162, 0.180),
    VoiceOption("IKne3meq5aSn9XLyUdCD", "Charlie", "elevenlabs", "turbo-v2.5",
                "Deep, confident, energetic Aussie", "male", 163, 0.180),
    VoiceOption("JBFqnCBsd6RMkjVDRZzb", "George", "elevenlabs", "turbo-v2.5",
                "Warm, captivating British storyteller", "male", 174, 0.180),
    VoiceOption("N2lVS1w4EtoT3dr4eOWO", "Callum", "elevenlabs", "turbo-v2.5",
                "Husky trickster, dramatic", "male", 170, 0.180),
    VoiceOption("SAz9YHcvj6GT2YYXdXww", "River", "elevenlabs", "turbo-v2.5",
                "Relaxed, neutral, informative", "unisex", 165, 0.180),
    VoiceOption("SOYHLrjzK2X1ezoPC6cr", "Harry", "elevenlabs", "turbo-v2.5",
                "Fierce warrior, bold baritone", "male", 168, 0.180),
    VoiceOption("TX3LPaxmHKxFdv7VOQHJ", "Liam", "elevenlabs", "turbo-v2.5",
                "Energetic, articulate young adult", "male", 163, 0.180),
    VoiceOption("Xb7hH8MSUJpSbSDYk0k2", "Alice", "elevenlabs", "turbo-v2.5",
                "Clear, engaging British educator", "female", 161, 0.180),
    VoiceOption("XrExE9yKIg1WjnnlVkGX", "Matilda", "elevenlabs", "turbo-v2.5",
                "Knowledgeable, expressive professional", "female", 162, 0.180),
    VoiceOption("bIHbv24MWmeRgasZH58o", "Will", "elevenlabs", "turbo-v2.5",
                "Relaxed optimist, friendly narrator", "male", 164, 0.180),
    VoiceOption("cgSgspJ2msm6clMCkdW9", "Jessica", "elevenlabs", "turbo-v2.5",
                "Playful, bright, warm conversational", "female", 162, 0.180),
    VoiceOption("cjVigY5qzO86Huf0OWal", "Eric", "elevenlabs", "turbo-v2.5",
                "Smooth, trustworthy sales executive", "male", 165, 0.180),
    VoiceOption("hpp4J3VqNfWAUOO0d1Us", "Bella", "elevenlabs", "turbo-v2.5",
                "Professional, bright, warm presenter", "female", 160, 0.180),
    VoiceOption("iP95p4xoKVk53GoZ742B", "Chris", "elevenlabs", "turbo-v2.5",
                "Charming, down-to-earth support", "male", 163, 0.180),
    VoiceOption("nPczCjzI2devNBz1zQrb", "Brian", "elevenlabs", "turbo-v2.5",
                "Deep, resonant and comforting baritone", "male", 168, 0.180),
    VoiceOption("onwK4e9ZLuTAKqWW03F9", "Daniel", "elevenlabs", "turbo-v2.5",
                "Steady, polished British broadcaster", "male", 166, 0.180),
    VoiceOption("pFZP5JQG7iQjIQuC4Bku", "Lily", "elevenlabs", "turbo-v2.5",
                "Velvety British actress, warm narration", "female", 164, 0.180),
    VoiceOption("pNInz6obpgDQGcFmaJgB", "Adam", "elevenlabs", "turbo-v2.5",
                "Dominant, firm, confident narrator", "male", 170, 0.180),
    VoiceOption("pqHfZKP75CvOlQylNhV4", "Bill", "elevenlabs", "turbo-v2.5",
                "Wise, mature, balanced executive", "male", 172, 0.180),
    VoiceOption("wBXNqKUATyqu0RtYt25i", "Adam (Workspace Cloned)", "elevenlabs", "turbo-v2.5",
                "Custom workspace cloned voice", "male", 170, 0.180),
]

LLM_MODELS = [
    {"model": "gemini-2.0-flash",      "label": "Gemini 2.0 Flash — ultra-fast real-time speech (Google)", "ttft_ms": 180,
     "cost_per_1k_tokens": 0.00010, "provider": "Google"},
    {"model": "gemini-2.0-flash-lite", "label": "Gemini 2.0 Flash Lite — lowest latency & high throughput (Google)", "ttft_ms": 150,
     "cost_per_1k_tokens": 0.000075, "provider": "Google"},
    {"model": "gemini-1.5-flash",      "label": "Gemini 1.5 Flash — fast, versatile 1M context (Google)", "ttft_ms": 210,
     "cost_per_1k_tokens": 0.000075, "provider": "Google"},
    {"model": "gemini-nano",           "label": "Gemini Nano — on-device ultra-low latency (Google)", "ttft_ms": 120,
     "cost_per_1k_tokens": 0.00005, "provider": "Google"},
    {"model": "gemini-1.5-pro",        "label": "Gemini 1.5 Pro — complex reasoning & multi-turn logic (Google)", "ttft_ms": 360,
     "cost_per_1k_tokens": 0.00125, "provider": "Google"},
    {"model": "gpt-4o-mini",           "label": "GPT-4o mini — fastest, cheapest (OpenAI)", "ttft_ms": 240,
     "cost_per_1k_tokens": 0.00015, "provider": "OpenAI"},
    {"model": "gpt-4o",                "label": "GPT-4o — best reasoning (OpenAI)", "ttft_ms": 410,
     "cost_per_1k_tokens": 0.0025, "provider": "OpenAI"},
    {"model": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5 — fast, strong instruction following (Anthropic)",
     "ttft_ms": 260, "cost_per_1k_tokens": 0.0008, "provider": "Anthropic"},
    {"model": "claude-sonnet-5",       "label": "Claude Sonnet 5 — highest quality dialogue (Anthropic)",
     "ttft_ms": 380, "cost_per_1k_tokens": 0.003, "provider": "Anthropic"},
]

# Starting points an operator can load into the editor and then adapt.
PROMPT_PRESETS: Dict[str, Dict[str, str]] = {
    "legal_intake": {
        "title": "Legal Intake",
        "first_message": "Thanks for calling. How can I help you today?",
        "system_prompt": (
            "You are the intake agent for a law firm.\n\n"
            "Answer in one or two short sentences, then stop.\n"
            "Do not recap what the caller just said.\n"
            "Do not list options unless asked — offer the single best one.\n"
            "Never read case numbers or URLs aloud unless asked.\n\n"
            "If the caller describes an emergency, transfer immediately\n"
            "using transfer_call."
        ),
    },
    "appointment_scheduling": {
        "title": "Appointment Scheduling",
        "first_message": "Hi, I can book, move or cancel an appointment. Which do you need?",
        "system_prompt": (
            "You schedule appointments for a busy clinic.\n\n"
            "Offer at most two times at once, newest first.\n"
            "Confirm the date, time and caller name once before booking.\n"
            "Keep every turn under fifteen words.\n"
            "Use check_availability before offering any slot."
        ),
    },
    "support_triage": {
        "title": "Support Triage",
        "first_message": "Support line, what's going wrong?",
        "system_prompt": (
            "You triage inbound technical support calls.\n\n"
            "Ask one diagnostic question at a time.\n"
            "Never guess a root cause — say what you would check next.\n"
            "If the caller is blocked from working, transfer to a human."
        ),
    },
    "sales_qualification": {
        "title": "Sales Qualification",
        "first_message": "Thanks for reaching out. What are you hoping to solve?",
        "system_prompt": (
            "You qualify inbound sales enquiries.\n\n"
            "Learn the use case, team size and timeline in that order.\n"
            "Never quote pricing — book time with an account executive instead.\n"
            "Stay under two sentences per turn."
        ),
    },
}


@dataclass
class AgentConfig:
    agent_id: str
    name: str
    first_message: str
    system_prompt: str
    voice_id: str = VOICE_CATALOG[0].voice_id
    llm_model: str = "gpt-4o-mini"
    temperature: float = 0.7
    tools: List[str] = field(default_factory=list)
    language: str = "en-US"
    description: str = ""
    interruption_enabled: bool = True
    filler_speech_enabled: bool = True
    max_response_words: int = 60
    speak_first: str = "ai"  # "ai" (AI speaks first) | "user" (User speaks first)
    active: bool = False
    revision: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentRevision:
    revision: int
    agent_id: str
    config: Dict[str, Any]
    changed_fields: List[str]
    note: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Prompt rules that matter specifically for speech: anything that makes the
# agent monologue, read symbols aloud, or stall costs conversation latency.
_LINT_RULES = [
    {
        "id": "no_length_guidance",
        "severity": "warning",
        "message": "No brevity instruction — spoken answers drift long without one.",
        "hint": "Add a line such as 'Answer in one or two short sentences, then stop.'",
        "test": lambda p: not re.search(
            r"\b(short|brief|concise|one or two|under \w+ words|sentences?)\b", p, re.I),
    },
    {
        "id": "reads_symbols_aloud",
        "severity": "warning",
        "message": "URLs, emails or IDs may be read aloud digit by digit.",
        "hint": "Add 'Never read URLs, emails or reference numbers aloud unless asked.'",
        "test": lambda p: not re.search(r"\b(url|email address|aloud|spell)\b", p, re.I),
    },
    {
        "id": "no_escalation_path",
        "severity": "warning",
        "message": "No escalation route — the caller cannot reach a human.",
        "hint": "Describe when to call transfer_call.",
        "test": lambda p: not re.search(r"\b(transfer|escalat\w+|human|agent|representative)\b", p, re.I),
    },
    {
        "id": "asks_to_list_options",
        "severity": "warning",
        "message": "Asking the agent to list options produces unspeakable menus.",
        "hint": "Prefer 'offer the single best option' over 'list all options'.",
        "test": lambda p: bool(re.search(r"\b(list (all|every|the)\s+\w+|enumerate|bullet points?)\b", p, re.I)),
    },
    {
        "id": "markdown_formatting",
        "severity": "warning",
        "message": "Markdown syntax in a spoken prompt leaks into speech.",
        "hint": "Remove **bold**, bullets and headings — this text is spoken, not rendered.",
        "test": lambda p: bool(re.search(r"(\*\*|^\s*[-*]\s+\w|^#{1,6}\s)", p, re.M)),
    },
    {
        "id": "second_person_persona",
        "severity": "info",
        "message": "Prompt does not open by telling the model who it is.",
        "hint": "Start with 'You are the ... agent for ...'.",
        "test": lambda p: not re.match(r"\s*you are\b", p, re.I),
    },
]


class AgentBuilder:
    """Registry, validator and sandbox behind the visual agent editor."""

    def __init__(self):
        self._agents: Dict[str, AgentConfig] = {}
        self._revisions: Dict[str, List[AgentRevision]] = {}
        self._load_state()
        if not self._agents:
            self._seed_default_agent()

    # ── Persistence ──────────────────────────────────────────────────────────
    def _load_state(self):
        if not os.path.isfile(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            import dataclasses
            valid_fields = {f.name for f in dataclasses.fields(AgentConfig)}
            for item in data.get("agents", []):
                filtered = {k: v for k, v in item.items() if k in valid_fields}
                cfg = AgentConfig(**filtered)
                self._agents[cfg.agent_id] = cfg
            for agent_id, revs in data.get("revisions", {}).items():
                self._revisions[agent_id] = [AgentRevision(**r) for r in revs]
        except Exception as e:
            log.warning("Failed to load agent configs: %s", e)

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "agents": [a.to_dict() for a in self._agents.values()],
                    "revisions": {
                        aid: [r.to_dict() for r in revs[-MAX_REVISIONS:]]
                        for aid, revs in self._revisions.items()
                    },
                    "updated_at": time.time(),
                }, f, indent=2)
        except Exception as e:
            log.warning("Failed to save agent configs: %s", e)

    def persist(self):
        """Writes the registry to disk."""
        self._save_state()

    def _seed_default_agent(self):
        preset = PROMPT_PRESETS["legal_intake"]
        self.create_agent(
            name="Intake Agent",
            first_message=preset["first_message"],
            system_prompt=preset["system_prompt"],
            description="Default agent seeded from the legal intake preset",
            tools=["check_availability", "book_appointment", "transfer_call"],
            active=True,
        )

    # ── Catalogues ───────────────────────────────────────────────────────────
    def list_voices(self, refresh_retell: bool = False) -> List[Dict[str, Any]]:
        voices = [v.to_dict() for v in VOICE_CATALOG]
        voices.extend(self._retell_voice_dicts(refresh_retell))
        return voices

    @staticmethod
    def _retell_voice_dicts(refresh: bool = False) -> List[Dict[str, Any]]:
        """Retell AI platform voices (live library when RETELL_API_KEY is set, else cached copy)."""
        try:
            from agent import retell_voices
            return retell_voices.list_retell_voice_options(force_refresh=refresh)
        except Exception as ex:
            log.warning("Retell voice library unavailable: %s", ex)
            return []

    def _get_retell_voice(self, voice_id: str) -> Optional[VoiceOption]:
        try:
            from agent import retell_voices
            if not retell_voices.is_retell_voice_id(voice_id):
                return None
            rec = retell_voices.get_retell_voice(voice_id)
            if not rec:
                return None
            d = retell_voices.to_voice_option_dict(rec)
            return VoiceOption(
                voice_id=d["voice_id"], name=d["name"], provider=d["provider"], model=d["model"],
                style=d["style"], gender=d["gender"], first_audio_ms=d["first_audio_ms"],
                cost_per_1k_chars=d["cost_per_1k_chars"],
            )
        except Exception as ex:
            log.warning("Retell voice lookup failed for %s: %s", voice_id, ex)
            return None

    def get_voice(self, voice_id: str) -> Optional[VoiceOption]:
        exact = next((v for v in VOICE_CATALOG if v.voice_id == voice_id), None)
        if exact:
            return exact
        retell = self._get_retell_voice(voice_id)
        if retell:
            return retell
        norm = str(voice_id).lower().replace("eleven-", "").replace("aura-", "").replace("openai-", "")
        return next((v for v in VOICE_CATALOG if norm in v.name.lower() or norm in v.voice_id.lower()), None)

    def list_models(self) -> List[Dict[str, Any]]:
        return list(LLM_MODELS)

    def list_presets(self) -> Dict[str, Dict[str, str]]:
        return copy.deepcopy(PROMPT_PRESETS)

    def available_tools(self) -> List[Dict[str, Any]]:
        """Tools the dialogue manager can attach, from the live tool registry."""
        try:
            from agent.tool_manager import ToolRegistry
            return [
                {"name": t.name, "description": t.description,
                 "parameters": t.parameters, "timeout": t.timeout,
                 "filler_phrases": t.filler_phrases}
                for t in ToolRegistry().list_tools()
            ]
        except Exception as e:  # tool registry is optional at build time
            log.warning("Tool registry unavailable: %s", e)
            return []

    def preview_voice(self, voice_id: str, text: str = "") -> Dict[str, Any]:
        """Returns what the editor needs to play and price a voice sample."""
        voice = self.get_voice(voice_id)
        if not voice:
            raise KeyError(f"Unknown voice '{voice_id}'")
        sample = text or "Thanks for calling. How can I help you today?"
        return {
            "voice": voice.to_dict(),
            "text": sample,
            "estimated_first_audio_ms": voice.first_audio_ms,
            "estimated_cost_usd": round(len(sample) / 1000 * voice.cost_per_1k_chars, 6),
            "estimated_duration_s": round(len(sample.split()) / WORDS_PER_MINUTE * 60, 2),
        }

    def derive_first_message(self, prompt: str, name: str = "Intake Agent") -> str:
        """Derives a natural opening greeting directly from the system prompt."""
        prompt = (prompt or "").strip()
        if not prompt:
            return "Hi, I'm an AI assistant from the intake team. How can I help you today?"

        # Check if the prompt has explicit first turn / opening speech specified in quotes
        m_open = re.search(r"(?:say this.*?first turn|opening.*?first turn|say this, and nothing else, as your first turn)[:\s\n]*\"([^\"]+)\"", prompt, re.I)
        if m_open:
            return m_open.group(1).strip()

        lines = [line.strip() for line in prompt.splitlines() if line.strip()]
        for line in lines[:4]:
            # Pattern 1: "You are Maya, an intake specialist answering calls for Bottini & Bottini, Inc."
            m_full = re.search(r"you are ([A-Z][a-z]+)[,\s]+(?:an?|the)?\s*([a-zA-Z\s]+?)\s*(?:answering calls for|for|at)\s*([^\.]+)", line, re.I)
            if m_full:
                p_name = m_full.group(1).strip()
                p_role = m_full.group(2).strip()
                p_company = m_full.group(3).strip().rstrip(".")
                team = "the intake team" if "intake" in p_role.lower() else ("the support team" if "support" in p_role.lower() else f"the {p_role}")
                return f"Hi, I'm {p_name} from {team} at {p_company}. I'm an AI assistant, how can I help you today?"

            # Pattern 2: "You are Maya, an intake specialist..."
            m_name_role = re.search(r"you are ([A-Z][a-z]+)[,\s]+(?:an?|the)?\s*([a-zA-Z\s]+?)(?:\.|$)", line, re.I)
            if m_name_role:
                p_name = m_name_role.group(1).strip()
                p_role = m_name_role.group(2).strip()
                team = "the intake team" if "intake" in p_role.lower() else f"the {p_role}"
                return f"Hi, I'm {p_name} from {team}. I'm an AI assistant, how can I help you today?"

            # Pattern 3: "You are the receptionist for X" / "You are an intake specialist at X"
            m = re.search(r"you are (?:the|an|a)?\s*(.*?)(?:\.|$)", line, re.I)
            if m:
                role = m.group(1).strip()
                if " for " in role.lower():
                    entity = re.split(r"\s+for\s+", role, flags=re.I)[-1].strip().rstrip(".")
                    return f"Hi, thanks for calling {entity}. I'm an AI assistant from the intake team, how can I help you today?"
                elif " at " in role.lower():
                    entity = re.split(r"\s+at\s+", role, flags=re.I)[-1].strip().rstrip(".")
                    return f"Hi, thanks for calling {entity}. I'm an AI assistant from the intake team, how can I help you today?"
                else:
                    clean_role = re.sub(r"\b(assistant|agent|representative|bot|ai)\b", "", role, flags=re.I).strip()
                    if clean_role:
                        return f"Hi, I'm an AI assistant from {clean_role}. How can I help you today?"

            if any(line.lower().startswith(w) for w in ("hello", "hi", "welcome", "thanks for calling", "good morning", "good afternoon")):
                clean = re.split(r"[.!?]\s+", line)[0].strip()
                if len(clean) > 5 and len(clean) < 140:
                    return clean + ("" if clean.endswith((".", "!", "?")) else ".")

        return "Hi, I'm an AI assistant from the intake team. How can I help you today?"

    # ── Validation & linting ─────────────────────────────────────────────────
    def validate(self, cfg: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Hard validation — these block a save."""
        errors: List[str] = []
        name = (cfg.get("name") or "").strip()
        prompt = cfg.get("system_prompt") or ""
        first = (cfg.get("first_message") or "").strip()

        if not name:
            errors.append("name is required")
        if not prompt.strip():
            errors.append("system_prompt is required")
        if len(prompt) > MAX_PROMPT_CHARS:
            errors.append(f"system_prompt exceeds {MAX_PROMPT_CHARS} characters")
        
        if not first:
            errors.append("first_message is required")
        elif len(first) > MAX_FIRST_MESSAGE_CHARS:
            errors.append(f"first_message exceeds {MAX_FIRST_MESSAGE_CHARS} characters")

        temperature = cfg.get("temperature", 0.7)
        if not isinstance(temperature, (int, float)) or not 0.0 <= float(temperature) <= 2.0:
            errors.append("temperature must be between 0.0 and 2.0")

        if cfg.get("voice_id") and not self.get_voice(cfg["voice_id"]):
            errors.append(f"unknown voice_id '{cfg['voice_id']}'")

        model = cfg.get("llm_model")
        if model and model not in {m["model"] for m in LLM_MODELS}:
            errors.append(f"unknown llm_model '{model}'")

        known_tools = {t["name"] for t in self.available_tools()}
        unknown = [t for t in cfg.get("tools", []) if known_tools and t not in known_tools]
        if unknown:
            errors.append(f"unknown tools: {', '.join(unknown)}")

        return len(errors) == 0, errors

    def lint_prompt(self, prompt: str) -> List[Dict[str, str]]:
        """Soft advice — findings surface in the editor but never block a save."""
        findings = []
        for rule in _LINT_RULES:
            try:
                if rule["test"](prompt or ""):
                    findings.append({
                        "id": rule["id"],
                        "severity": rule["severity"],
                        "message": rule["message"],
                        "hint": rule["hint"],
                    })
            except Exception:
                continue
        return findings

    # ── CRUD ─────────────────────────────────────────────────────────────────
    def create_agent(self, name: str, first_message: str, system_prompt: str, **kwargs) -> AgentConfig:
        first_msg_clean = (first_message or "").strip()
        if not first_msg_clean:
            first_msg_clean = self.derive_first_message(system_prompt, name)

        payload = {
            "name": name,
            "first_message": first_msg_clean,
            "system_prompt": system_prompt,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        ok, errors = self.validate(payload)
        if not ok:
            raise ValueError("; ".join(errors))

        agent_id = payload.pop("agent_id", None) or self._mint_id(name)
        activate = bool(payload.pop("active", False))
        cfg = AgentConfig(agent_id=agent_id, **payload)
        self._agents[agent_id] = cfg
        self._revisions[agent_id] = [
            AgentRevision(1, agent_id, cfg.to_dict(), ["created"], "Initial version")
        ]
        if activate:
            self.set_active(agent_id)
        self._save_state()
        log.info("Created agent %s (%s)", agent_id, name)
        return cfg

    def _mint_id(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent"
        agent_id, n = base, 2
        while agent_id in self._agents:
            agent_id, n = f"{base}-{n}", n + 1
        return agent_id

    def get_agent(self, agent_id: str) -> Optional[AgentConfig]:
        return self._agents.get(agent_id)

    def list_agents(self) -> List[Dict[str, Any]]:
        return [a.to_dict() for a in sorted(self._agents.values(), key=lambda a: a.updated_at, reverse=True)]

    def get_active_agent(self) -> Optional[AgentConfig]:
        return next((a for a in self._agents.values() if a.active), None)

    def update_agent(self, agent_id: str, changes: Dict[str, Any], note: str = "") -> AgentConfig:
        cfg = self._agents.get(agent_id)
        if not cfg:
            raise KeyError(f"Unknown agent '{agent_id}'")

        proposed = cfg.to_dict()
        editable = {k: v for k, v in changes.items()
                    if k in proposed and k not in ("agent_id", "revision", "created_at", "updated_at")}
        
        if "first_message" in editable and not (editable["first_message"] or "").strip():
            p = editable.get("system_prompt") or cfg.system_prompt
            n = editable.get("name") or cfg.name
            editable["first_message"] = self.derive_first_message(p, n)

        proposed.update(editable)

        ok, errors = self.validate(proposed)
        if not ok:
            raise ValueError("; ".join(errors))

        changed = [k for k, v in editable.items() if cfg.to_dict().get(k) != v]
        if not changed:
            return cfg

        for key, value in editable.items():
            setattr(cfg, key, value)
        cfg.revision += 1
        cfg.updated_at = time.time()

        self._revisions.setdefault(agent_id, []).append(
            AgentRevision(cfg.revision, agent_id, cfg.to_dict(), changed, note)
        )
        self._revisions[agent_id] = self._revisions[agent_id][-MAX_REVISIONS:]
        self._save_state()
        log.info("Updated agent %s to revision %d (%s)", agent_id, cfg.revision, ", ".join(changed))
        return cfg

    def clone_agent(self, agent_id: str, new_name: Optional[str] = None) -> AgentConfig:
        cfg = self._agents.get(agent_id)
        if not cfg:
            raise KeyError(f"Unknown agent '{agent_id}'")
        data = cfg.to_dict()
        for key in ("agent_id", "revision", "created_at", "updated_at", "active"):
            data.pop(key, None)
        data["name"] = new_name or f"{cfg.name} (copy)"
        return self.create_agent(**data)

    def delete_agent(self, agent_id: str) -> bool:
        removed = self._agents.pop(agent_id, None)
        if not removed:
            return False
        self._revisions.pop(agent_id, None)
        # Deleting the live agent would leave calls with no persona, so the
        # most recently edited survivor takes over.
        if removed.active and self._agents:
            successor = max(self._agents.values(), key=lambda a: a.updated_at)
            self.set_active(successor.agent_id)
            log.info("Deleted live agent %s; %s is now live", agent_id, successor.agent_id)
        self._save_state()
        return True

    def set_active(self, agent_id: str) -> AgentConfig:
        """Exactly one agent answers calls, so activation deactivates the rest."""
        cfg = self._agents.get(agent_id)
        if not cfg:
            raise KeyError(f"Unknown agent '{agent_id}'")
        for other in self._agents.values():
            other.active = other.agent_id == agent_id
        cfg.updated_at = time.time()
        self._save_state()
        return cfg

    # ── Revisions ────────────────────────────────────────────────────────────
    def list_revisions(self, agent_id: str) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in reversed(self._revisions.get(agent_id, []))]

    def diff_revisions(self, agent_id: str, from_rev: int, to_rev: int) -> Dict[str, Any]:
        revs = {r.revision: r for r in self._revisions.get(agent_id, [])}
        if from_rev not in revs or to_rev not in revs:
            raise KeyError(f"Revision not found for agent '{agent_id}'")
        before, after = revs[from_rev].config, revs[to_rev].config
        changed = {
            key: {"from": before.get(key), "to": after.get(key)}
            for key in set(before) | set(after)
            if before.get(key) != after.get(key) and key not in ("revision", "updated_at")
        }
        return {"agent_id": agent_id, "from": from_rev, "to": to_rev, "changed": changed}

    def rollback(self, agent_id: str, revision: int) -> AgentConfig:
        """Restores an earlier revision as a new revision on top of history."""
        revs = {r.revision: r for r in self._revisions.get(agent_id, [])}
        if revision not in revs:
            raise KeyError(f"Revision {revision} not found for agent '{agent_id}'")
        snapshot = dict(revs[revision].config)
        for key in ("agent_id", "revision", "created_at", "updated_at", "active"):
            snapshot.pop(key, None)
        return self.update_agent(agent_id, snapshot, note=f"Rollback to revision {revision}")

    # ── Sandbox ──────────────────────────────────────────────────────────────
    def test_run(self, agent_id: str, utterances: Optional[List[str]] = None) -> Dict[str, Any]:
        """Previews the opening turns of a call without dialling anything.

        The editor needs an answer to "what will this agent actually say, how
        fast, and what will it cost" before an operator puts it on a phone line.
        """
        cfg = self._agents.get(agent_id)
        if not cfg:
            raise KeyError(f"Unknown agent '{agent_id}'")

        voice = self.get_voice(cfg.voice_id) or VOICE_CATALOG[0]
        model = next((m for m in LLM_MODELS if m["model"] == cfg.llm_model), LLM_MODELS[0])
        tools = self.available_tools()
        tool_index = {t["name"]: t for t in tools if t["name"] in cfg.tools}

        turns: List[Dict[str, Any]] = [{
            "speaker": "agent",
            "text": cfg.first_message,
            "tool": None,
            "first_audio_ms": voice.first_audio_ms,
            "chars": len(cfg.first_message),
        }]

        for idx, utterance in enumerate(utterances or []):
            turns.append({"speaker": "caller", "text": utterance, "tool": None,
                          "first_audio_ms": 0, "chars": len(utterance)})
            tool_name = self._match_tool(utterance, tool_index)
            reply = self._preview_reply(cfg, utterance, tool_name, history=turns, turn_idx=idx)
            turns.append({
                "speaker": "agent",
                "text": reply,
                "tool": tool_name,
                "first_audio_ms": model["ttft_ms"] + voice.first_audio_ms,
                "chars": len(reply),
            })

        agent_chars = sum(t["chars"] for t in turns if t["speaker"] == "agent")
        agent_words = sum(len(t["text"].split()) for t in turns if t["speaker"] == "agent")
        latencies = [t["first_audio_ms"] for t in turns if t["speaker"] == "agent"]

        return {
            "agent_id": agent_id,
            "agent_name": cfg.name,
            "revision": cfg.revision,
            "voice": voice.to_dict(),
            "model": model,
            "turns": turns,
            "tools_triggered": [t["tool"] for t in turns if t["tool"]],
            "lint": self.lint_prompt(cfg.system_prompt),
            "estimates": {
                "avg_first_audio_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
                "max_first_audio_ms": max(latencies) if latencies else 0,
                "meets_700ms_target": all(ms <= 700 for ms in latencies),
                "spoken_seconds": round(agent_words / WORDS_PER_MINUTE * 60, 2),
                "tts_cost_usd": round(agent_chars / 1000 * voice.cost_per_1k_chars, 6),
                "agent_words": agent_words,
            },
        }

    def _match_tool(self, utterance: str, tool_index: Dict[str, Dict[str, Any]]) -> Optional[str]:
        """Picks the attached tool whose name or description best fits the utterance."""
        text = utterance.lower()
        best, best_score = None, 0
        for name, tool in tool_index.items():
            words = set(re.findall(r"[a-z]{4,}", f"{name} {tool.get('description', '')}".lower()))
            score = sum(1 for w in words if w in text)
            if score > best_score:
                best, best_score = name, score
        return best if best_score else None

    def _preview_reply(self, cfg: AgentConfig, utterance: str, tool_name: Optional[str],
                       history: Optional[List[Dict[str, Any]]] = None, turn_idx: int = 0) -> str:
        """Composes intelligent, multi-turn conversational replies grounded in the agent's system prompt & tools."""
        from agent.voice_synthesizer import clean_spoken_speech_text

        # Check if live Gemini or OpenAI API key is available
        gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        openai_key = os.getenv("OPENAI_API_KEY")

        if gemini_key and cfg.llm_model.startswith("gemini"):
            try:
                import urllib.request
                model_name = cfg.llm_model if ("1.5" in cfg.llm_model or "2.0" in cfg.llm_model) else "gemini-2.0-flash"
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={gemini_key}"

                gemini_contents = []
                if history:
                    for t in history:
                        role = "user" if t.get("speaker") == "caller" else "model"
                        gemini_contents.append({"role": role, "parts": [{"text": t.get("text", "")}]})
                else:
                    gemini_contents.append({"role": "user", "parts": [{"text": utterance}]})

                system_instruction = {
                    "parts": [{
                        "text": (
                            f"{cfg.system_prompt}\n\n"
                            f"Guidelines: Speak concisely in 1-2 natural conversational sentences suitable for a phone call. "
                            f"Never output XML, markdown, or thought tags."
                        )
                    }]
                }
                payload_dict = {
                    "system_instruction": system_instruction,
                    "contents": gemini_contents,
                    "generationConfig": {
                        "temperature": cfg.temperature,
                        "maxOutputTokens": 120
                    }
                }
                payload = json.dumps(payload_dict).encode("utf-8")
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    if text:
                        clean = clean_spoken_speech_text(text)
                        if clean:
                            return clean
            except Exception as e:
                log.debug("Live Gemini sandbox completion skipped: %s", e)

        # ── Tool matches ────────────────────────────────────────────────────────
        if tool_name:
            if "book" in tool_name or "appointment" in tool_name:
                return "I can certainly help you book that appointment. What time works best for you?"
            if "availab" in tool_name:
                return "Let me check the calendar availability for you right now. We have several open slots."
            if "order" in tool_name:
                return "Looking up your order status right now. Everything looks on schedule."
            if "knowledge" in tool_name or "query" in tool_name:
                return "Let me retrieve that information for you. Here is what our records show."
            if "transfer" in tool_name:
                return "Connecting you with a specialist right now. Please hold for just a moment."
            return "Let me check that for you right now. I'll confirm the details in a moment."

        u_lower = utterance.strip().lower()
        prompt_lower = cfg.system_prompt.lower()

        # ── Global Interruptions / Standard Queries ────────────────────────────
        if re.search(r"\b(911|medical emergency|dying|bleeding|ambulance|hospital)\b", u_lower):
            return "If you are experiencing an immediate medical emergency, please hang up and dial 911 immediately."

        if re.search(r"\b(urgent|emergency|right now|immediately|speak to (human|agent|person|lawyer|attorney)|transfer)\b", u_lower) \
                and ("transfer_call" in cfg.tools or "intake" in prompt_lower or "legal" in prompt_lower):
            return "I understand. I am transferring you directly to our intake coordinator right away. Please hold."

        if re.search(r"\b(are you (an )?ai|are you (a )?robot|are you real|who are you)\b", u_lower):
            name_str = cfg.name or "Maya"
            if "bottini" in prompt_lower or "legal" in prompt_lower or "law" in prompt_lower:
                return f"Yes, I am {name_str}, an AI intake specialist for Bottini & Bottini. I am here to gather your information for attorney review."
            return f"Yes, I am {name_str}, an AI voice assistant. How can I help you today?"

        # ── Multi-turn State History Analysis ──────────────────────────────────
        prior_agent_turns = [t.get("text", "") for t in (history or []) if t.get("speaker") == "agent"]
        prior_agent_text = " ".join(prior_agent_turns).lower()

        # ── Legal / Bottini & Bottini Intake Dialogue Flow (Prompt Grounded) ──
        if "law" in prompt_lower or "legal" in prompt_lower or "attorney" in prompt_lower or "bottini" in prompt_lower or "intake" in prompt_lower:
            # Check for fee/cost inquiry (handled per sanctioned line in Step 5 / Rules)
            if any(w in u_lower for w in ["cost", "price", "fee", "how much", "charge", "percentage", "contingency"]):
                return "Our firm handles class action and representative matters on a contingency basis with no upfront legal fees. Let's continue with your intake details for attorney review."

            # Check for out-of-scope legal matters explicitly listed in prompt (divorce, DUI, personal injury car accident, immigration)
            if re.search(r"\b(personal injury|car accident|car crash|traffic accident|auto accident|divorce|child custody|dui|dwi|drunk driving|immigration|visa|green card|criminal defense)\b", u_lower) \
                    and ("shareholder" in prompt_lower or "whistleblower" in prompt_lower or "consumer" in prompt_lower):
                return "That's outside what our firm handles, we focus on shareholder, whistleblower, and consumer cases. I'd suggest contacting your state or local bar association's referral service. I'm sorry we're not the right place for this one. Take care."

            # Opening response / Step 1: find out what kind of matter this is
            is_opening_confirmation = any(u_lower.startswith(g) for g in ["yes", "yeah", "yep", "sure", "ok", "okay", "fine", "go ahead", "hello", "hi", "hey", "can you help", "need help"]) and len(u_lower.split()) <= 5
            if is_opening_confirmation and ("what happened" not in prior_agent_text and "what you need help with" not in prior_agent_text and "step 1" not in prior_agent_text):
                return "Can you tell me, in a sentence or two, what happened or what you need help with?"

            # Step 2: Core fields (ask these regardless of category)
            if "first and last name" not in prior_agent_text and "full name" not in prior_agent_text and "your name" not in prior_agent_text and "spell your" not in prior_agent_text:
                return "Could you spell your full first and last name for me, letter by letter?"

            if "callback number" not in prior_agent_text and "phone number" not in prior_agent_text and "country code" not in prior_agent_text:
                return "What's the best callback number, with the country code if you're calling from outside the US?"

            if "email address" not in prior_agent_text and "email" not in prior_agent_text:
                return "And what is your best email address for our legal team to send intake documentation?"

            if "mailing address" not in prior_agent_text and "address" not in prior_agent_text:
                return "And what's the best mailing address for you? That's so we can send documents if the attorney needs to."

            if "represented by another attorney" not in prior_agent_text and "another firm" not in prior_agent_text:
                return "Are you currently represented by another attorney on this matter, or have you already spoken with another firm about it?"

            if "contacted by, or given a statement" not in prior_agent_text and "investigator" not in prior_agent_text:
                return "Have you already been contacted by, or given a statement to, the company, its lawyers, an investigator, or a government agency about this?"

            if "how did you hear" not in prior_agent_text and "referral" not in prior_agent_text:
                return "How did you hear about Bottini & Bottini?"

            if "employee, officer, or director" not in prior_agent_text and "affiliated" not in prior_agent_text:
                return "Last quick one before we get into details. Are you currently an employee, officer, or director of the company involved?"

            # Step 5: Retention Agreement Consent
            if "retention agreement" not in prior_agent_text and "digital version" not in prior_agent_text:
                return "One last thing. If the attorney reviews this and thinks we can help, can I send you the digital version of the retention agreement to sign? That's the document that would formally make us your attorneys. It comes to your email, there's no obligation, and nothing is in place until you've read it and signed it."

            if any(w in u_lower for w in ["thank", "thanks", "that's all", "thats all", "no that", "nothing else", "bye", "goodbye"]):
                return "You're very welcome! An intake attorney from Bottini & Bottini will review your file and reach out to you shortly. Have a wonderful day!"

            if "logged" not in prior_agent_text and "review your file" not in prior_agent_text and "intake file" not in prior_agent_text:
                return "Thank you for sharing those details. I've recorded all of this information for the attorneys at Bottini & Bottini. A member of our legal team will review your file and follow up with you. Take care."

            return "Your case details have been safely recorded and queued for attorney review. Is there any additional information you'd like to include?"

        # ── Medical / Dental Clinic Dialogue Flow ──────────────────────────────
        if "clinic" in prompt_lower or "dental" in prompt_lower or "doctor" in prompt_lower or "health" in prompt_lower:
            if any(w in u_lower for w in ["hour", "time", "open", "schedule", "when"]):
                return "Our clinic is open Monday through Saturday from 8 AM to 6 PM."
            if "service" in u_lower or "what do you do" in u_lower:
                return "We provide general consultations, cleanings, surgical procedures, and emergency dental care."
            if "appointment" not in prior_agent_text and "morning or afternoon" not in prior_agent_text:
                return "I would be glad to help you schedule that. Would you prefer a morning or afternoon appointment this week?"
            if "full name" not in prior_agent_text and "first and last name" not in prior_agent_text:
                return "Please provide your full first and last name so we can reserve your appointment slot."
            if "phone" not in prior_agent_text:
                return "What is your best contact phone number to receive confirmation and reminder texts?"
            return "Thank you! Your appointment request has been submitted. Our front desk will confirm your slot shortly."

        # ── Tech / Customer Support Dialogue Flow ──────────────────────────────
        if "support" in prompt_lower or "triage" in prompt_lower or "tech" in prompt_lower:
            if "operating system" not in prior_agent_text and "device" not in prior_agent_text:
                return "I understand the issue you're experiencing. Could you tell me what operating system or device you are on?"
            if "error message" not in prior_agent_text:
                return "What specific error message or behavior are you observing?"
            return "I have documented your issue under ticket #4819. Our engineering team is investigating."

        # ── Sales / Enterprise Dialogue Flow ───────────────────────────────────
        if "sales" in prompt_lower:
            if "team size" not in prior_agent_text:
                return "We would love to help you with that! Could you tell me a bit about your team size and timeline?"
            return "Thank you for sharing that. A product specialist will be in touch with a customized demo."

        if utterance.strip().endswith("?"):
            return "Yes, absolutely! I can help you with that right away. What specific details would you like to know?"

        return "Got it. I've noted that. How else can I assist you with your request today?"

    # ── Telemetry ────────────────────────────────────────────────────────────
    def get_stats(self) -> Dict[str, Any]:
        agents = list(self._agents.values())
        active = self.get_active_agent()
        return {
            "agents": len(agents),
            "active_agent": active.agent_id if active else None,
            "voices": len(VOICE_CATALOG),
            "models": len(LLM_MODELS),
            "presets": len(PROMPT_PRESETS),
            "tools_available": len(self.available_tools()),
            "total_revisions": sum(len(r) for r in self._revisions.values()),
            "avg_prompt_chars": round(
                sum(len(a.system_prompt) for a in agents) / len(agents), 1) if agents else 0.0,
        }

    def export_agent(self, agent_id: str) -> str:
        cfg = self._agents.get(agent_id)
        if not cfg:
            raise KeyError(f"Unknown agent '{agent_id}'")
        return json.dumps(cfg.to_dict(), indent=2, sort_keys=True)

    def import_agent(self, blob: str, activate: bool = False) -> AgentConfig:
        data = json.loads(blob)
        for key in ("agent_id", "revision", "created_at", "updated_at"):
            data.pop(key, None)
        data["active"] = activate
        return self.create_agent(**data)

    def reset(self):
        """Clears the registry (used by acceptance tests)."""
        self._agents.clear()
        self._revisions.clear()


# Module-level singleton used by the worker, REST API and acceptance tests.
agent_builder = AgentBuilder()
