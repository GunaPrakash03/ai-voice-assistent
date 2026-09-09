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

MAX_PROMPT_CHARS = 6000
MAX_FIRST_MESSAGE_CHARS = 400
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


# Voices the TTS layer (agent/tts_manager.py) can already speak with.
VOICE_CATALOG: List[VoiceOption] = [
    VoiceOption("f786b574-daa5-4673-aa0c-cbe3e8534c02", "Aurora", "cartesia", "sonic-3",
                "warm, measured", "female", 88, 0.075),
    VoiceOption("a0e99841-438c-4a64-b679-ae501e7d6091", "Vale", "cartesia", "sonic-3",
                "brisk, neutral", "female", 84, 0.075),
    VoiceOption("729651dc-c6c3-4ee5-97fa-350da1f88600", "Ridge", "cartesia", "sonic-3",
                "deep, formal", "male", 91, 0.075),
    VoiceOption("aura-asteria-en", "Asteria", "deepgram", "aura-2",
                "bright, conversational", "female", 132, 0.030),
    VoiceOption("aura-orion-en", "Orion", "deepgram", "aura-2",
                "steady, authoritative", "male", 139, 0.030),
    VoiceOption("aura-luna-en", "Luna", "deepgram", "aura-2",
                "soft, empathetic", "female", 128, 0.030),
]

LLM_MODELS = [
    {"model": "gpt-4o-mini",   "label": "GPT-4o mini — fastest, cheapest", "ttft_ms": 240,
     "cost_per_1k_tokens": 0.00015},
    {"model": "gpt-4o",        "label": "GPT-4o — best reasoning",         "ttft_ms": 410,
     "cost_per_1k_tokens": 0.0025},
    {"model": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5 — fast, strong instruction following",
     "ttft_ms": 260, "cost_per_1k_tokens": 0.0008},
    {"model": "claude-sonnet-5", "label": "Claude Sonnet 5 — highest quality dialogue",
     "ttft_ms": 380, "cost_per_1k_tokens": 0.003},
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
            for item in data.get("agents", []):
                cfg = AgentConfig(**item)
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
    def list_voices(self) -> List[Dict[str, Any]]:
        return [v.to_dict() for v in VOICE_CATALOG]

    def get_voice(self, voice_id: str) -> Optional[VoiceOption]:
        return next((v for v in VOICE_CATALOG if v.voice_id == voice_id), None)

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
                 "parameters": t.parameters, "timeout": t.timeout}
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

    # ── Validation & linting ─────────────────────────────────────────────────
    def validate(self, cfg: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Hard validation — these block a save."""
        errors: List[str] = []
        name = (cfg.get("name") or "").strip()
        prompt = cfg.get("system_prompt") or ""
        first = cfg.get("first_message") or ""

        if not name:
            errors.append("name is required")
        if not prompt.strip():
            errors.append("system_prompt is required")
        if len(prompt) > MAX_PROMPT_CHARS:
            errors.append(f"system_prompt exceeds {MAX_PROMPT_CHARS} characters")
        if not first.strip():
            errors.append("first_message is required")
        if len(first) > MAX_FIRST_MESSAGE_CHARS:
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
        payload = {
            "name": name,
            "first_message": first_message,
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

        for utterance in (utterances or []):
            turns.append({"speaker": "caller", "text": utterance, "tool": None,
                          "first_audio_ms": 0, "chars": len(utterance)})
            tool_name = self._match_tool(utterance, tool_index)
            reply = self._preview_reply(cfg, utterance, tool_name)
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

    def _preview_reply(self, cfg: AgentConfig, utterance: str, tool_name: Optional[str]) -> str:
        """Composes the reply shape the prompt asks for, without calling an LLM."""
        if tool_name:
            return (f"Let me check that for you. [{tool_name}] "
                    f"I'll confirm the details in a moment.")
        if re.search(r"\b(urgent|emergency|right now|immediately)\b", utterance, re.I) \
                and "transfer_call" in cfg.tools:
            return "This sounds urgent — I'm putting you through to someone now. [transfer_call]"
        if utterance.strip().endswith("?"):
            return "Yes — here's the short answer, and I can go into detail if you want."
        return "Understood. What else can I help with?"

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
