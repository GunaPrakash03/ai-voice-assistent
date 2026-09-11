"""
Task 1.4 — Streaming LLM Dialogue Manager.

Components:
1. Streaming Model Wrapper (OpenAI GPT-4o-mini / Gemini / local simulation mode).
2. Clause Boundary Splitter (splits streaming tokens at punctuation boundaries for sub-200ms TTFT to downstream TTS).
3. Conversation History Context Buffer (multi-turn context buffer with sliding window, role tracking, and serialization).
4. Instant Barge-in Cancellation (instantly halts token generation when user interrupts).
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterable, Callable, List, Optional

from agent.tool_manager import AsyncToolDispatcher, ToolRegistry

log = logging.getLogger("llm-manager")


@dataclass
class Message:
    role: str  # "system", "user", "assistant"
    content: str
    timestamp: float = field(default_factory=time.time)
    interrupted: bool = False
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "interrupted": self.interrupted,
            "metadata": self.metadata,
        }


class ConversationContextBuffer:
    """
    Manages multi-turn conversation history with sliding window,
    token budgeting, and serialization for WebRTC data broadcasting.
    """

    def __init__(
        self,
        system_instruction: str = "You are a concise, helpful AI voice assistant.",
        max_turns: int = 20,
        max_tokens: int = 4000,
    ) -> None:
        self.system_instruction = system_instruction
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self._history: List[Message] = []

    def add_user_message(self, content: str, metadata: Optional[dict] = None) -> Message:
        msg = Message(role="user", content=content.strip(), metadata=metadata or {})
        self._history.append(msg)
        self._trim()
        return msg

    def add_assistant_message(
        self, content: str, interrupted: bool = False, metadata: Optional[dict] = None
    ) -> Message:
        msg = Message(
            role="assistant",
            content=content.strip(),
            interrupted=interrupted,
            metadata=metadata or {},
        )
        self._history.append(msg)
        self._trim()
        return msg

    def _trim(self) -> None:
        """Keep context within turn limit and approximate token limit."""
        if len(self._history) > self.max_turns:
            self._history = self._history[-self.max_turns:]

        # Simple token estimation (~4 chars per token)
        total_tokens = sum(len(m.content) // 4 for m in self._history)
        while total_tokens > self.max_tokens and len(self._history) > 2:
            dropped = self._history.pop(0)
            total_tokens -= len(dropped.content) // 4

    def get_messages_for_llm(self) -> List[dict]:
        """Returns standard chat completion message format."""
        msgs = [{"role": "system", "content": self.system_instruction}]
        for m in self._history:
            content = m.content
            if m.interrupted:
                content += " [caller interrupted]"
            msgs.append({"role": m.role, "content": content})
        return msgs

    def clear(self) -> None:
        self._history.clear()

    def to_list(self) -> List[dict]:
        return [m.to_dict() for m in self._history]

    def __len__(self) -> int:
        return len(self._history)


class ClauseBoundarySplitter:
    """
    Clause Boundary Splitter.

    Breaks streaming tokens at clause/sentence boundaries (., ?, !, ,, ;, :, —, \\n)
    so downstream components (like Task 1.5 Cartesia TTS or browser transcript streams)
    can begin immediately with sub-200ms TTFT rather than waiting for full sentences.
    """

    # Strong punctuation: sentence ends
    STRONG_PUNCT = {".", "?", "!", "\n"}
    # Weak punctuation: natural conversational clause pauses
    WEAK_PUNCT = {",", ";", ":", "—", "-", "..."}

    def __init__(
        self,
        min_clause_chars: int = 14,
        min_clause_words: int = 3,
        max_buffer_chars: int = 90,
    ) -> None:
        self.min_clause_chars = min_clause_chars
        self.min_clause_words = min_clause_words
        self.max_buffer_chars = max_buffer_chars
        self._buffer: str = ""
        self._clause_count: int = 0

    def feed_token(self, token: str) -> List[str]:
        """
        Feeds a newly arrived token from the LLM stream.
        Returns a list of completed clauses, if any boundary was reached.
        """
        self._buffer += token
        clauses: List[str] = []

        while True:
            clause = self._extract_clause()
            if clause:
                self._clause_count += 1
                clauses.append(clause)
            else:
                break

        return clauses

    def _extract_clause(self) -> Optional[str]:
        text = self._buffer
        if not text:
            return None

        # Look for delimiter positions
        for i, char in enumerate(text):
            is_strong = char in self.STRONG_PUNCT
            is_weak = char in self.WEAK_PUNCT

            if is_strong or is_weak:
                candidate = text[: i + 1].strip()
                words = candidate.split()

                # Strong punctuation splits if there is at least 1 word
                if is_strong and len(words) >= 1:
                    self._buffer = text[i + 1:].lstrip()
                    return candidate

                # Weak punctuation splits if candidate meets min words/chars criteria
                if is_weak and (
                    len(candidate) >= self.min_clause_chars
                    or len(words) >= self.min_clause_words
                ):
                    self._buffer = text[i + 1:].lstrip()
                    return candidate

        # Force split on space if buffer is getting too long (avoid TTS latency starvation)
        if len(text) >= self.max_buffer_chars:
            last_space = text.rfind(" ")
            if last_space > self.min_clause_chars:
                candidate = text[:last_space].strip()
                self._buffer = text[last_space + 1:].lstrip()
                return candidate

        return None

    def flush(self) -> List[str]:
        """Flushes any remaining text in the buffer as the final clause."""
        remainder = self._buffer.strip()
        self._buffer = ""
        if remainder:
            self._clause_count += 1
            return [remainder]
        return []

    def reset(self) -> None:
        self._buffer = ""
        self._clause_count = 0


class StreamingDialogueManager:
    """
    Streaming LLM Dialogue Manager.

    Coordinates:
    - Context buffer management.
    - LLM streaming invocation (OpenAI GPT-4o-mini / simulation fallback).
    - Real-time token streaming with clause boundary splitting.
    - Turn cancellation upon barge-in interruption.
    - Latency metrics tracking (TTFT, total generation time, tokens).
    """

    def __init__(
        self,
        system_instruction: str = (
            "You are a friendly, helpful, and concise AI voice assistant. "
            "Speak naturally in conversational English. Keep answers brief (1 to 3 sentences) "
            "suitable for spoken dialogue without bullet points, markdown formatting, or emojis."
        ),
        model: str = "gpt-4o-mini",
        api_key: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 150,
    ) -> None:
        self.system_instruction = system_instruction
        self.model = model
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "").strip()
        self.temperature = temperature
        self.max_tokens = max_tokens

        self.context = ConversationContextBuffer(system_instruction=system_instruction)
        self.active_generation_task: Optional[asyncio.Task] = None
        self._is_mock = not bool(self.api_key)

        self.tool_registry = ToolRegistry()
        self.tool_dispatcher = AsyncToolDispatcher(self.tool_registry)

        if not self._is_mock:
            log.info("Initialized StreamingDialogueManager with OpenAI model=%s", self.model)
        else:
            log.info(
                "OPENAI_API_KEY not set — using local conversational streaming simulator"
            )

    @property
    def is_mock(self) -> bool:
        return self._is_mock

    def _detect_simulated_tool_call(self, text: str) -> Optional[tuple[str, dict]]:
        lower = text.lower()

        # Extract doctor / specialty
        doctor_name = "Dr. Mohan" if "mohan" in lower else "Dr. Sarah"
        specialty = "Cardiology" if ("cardio" in lower or "heart" in lower) else ("General Consultation" if "consult" in lower else "Specialist Care")
        
        # Extract date
        date = "Friday, September 11" if ("friday" in lower or "september 11" in lower or "11" in lower) else ("today" if "today" in lower else "tomorrow")

        # Extract time
        time_slot = "11:00 AM"
        if "9:30" in lower or "nine thirty" in lower or "9 30" in lower:
            time_slot = "09:30 AM"
        elif "3:00" in lower or "three pm" in lower or "3 pm" in lower or "3" in lower:
            time_slot = "03:00 PM"
        elif "2:15" in lower or "two fifteen" in lower:
            time_slot = "02:15 PM"

        if "order" in lower or "tracking" in lower or "package" in lower:
            m = re.search(r'(?:order|tracking|#|number|id)\s*([a-zA-Z0-9\-]+)', lower)
            order_id = m.group(1).upper() if m and m.group(1).isalnum() else "1042"
            return ("lookup_order", {"order_id": order_id})
        elif "available" in lower or "availability" in lower or "schedule" in lower or "free slots" in lower or "openings" in lower or "is or not" in lower or "is available" in lower:
            service = f"{specialty} with {doctor_name}"
            return ("check_availability", {"service_type": service, "date": date})
        elif ("book" in lower or "reserve" in lower or "reservation" in lower or "appointment" in lower or "choose" in lower or "want to choose" in lower or "make an appointment" in lower) and not ("no" in lower and len(lower.split()) <= 4):
            return ("book_appointment", {
                "name": "Valued Caller",
                "phone": "555-0199",
                "date": date,
                "time_slot": time_slot,
                "service": f"{specialty} with {doctor_name}",
            })
        elif "return" in lower or "refund" in lower or "hour" in lower or "pricing" in lower or "cost" in lower or "policy" in lower:
            return ("query_knowledge_base", {"query": text})
        elif "webhook" in lower or "external api" in lower or "dispatch" in lower:
            return ("execute_webhook", {
                "endpoint_url": "mock://api/voice/events",
                "method": "POST",
                "payload": {"event": "call_inquiry", "prompt": text},
            })
        elif "transfer" in lower or "speak to human" in lower or "operator" in lower or "representative" in lower:
            dept = "support"
            if "billing" in lower:
                dept = "billing"
            elif "sales" in lower:
                dept = "sales"
            elif "tech" in lower or "engineer" in lower:
                dept = "technical support"
            mode = "warm" if ("warm" in lower or "brief" in lower) else "blind"
            return ("transfer_call", {
                "destination": "+18885550142",
                "transfer_type": mode,
                "department": dept,
                "reason": "Caller requested human assistance",
            })
        return None

    def _format_grounded_tool_response(self, tool_name: str, tool_result: dict) -> str:
        res = tool_result.get("result") or {}
        if tool_result.get("status") != "success":
            return "I apologize, but I am having trouble accessing that system right now. I can note your details and have our team follow up with you."

        if tool_name == "lookup_order":
            return (
                f"I looked up order {res.get('order_id')}. It is currently {res.get('status')} "
                f"with {res.get('carrier')} tracking number {res.get('tracking_number')}, "
                f"and is scheduled for delivery {res.get('estimated_delivery')}."
            )
        elif tool_name == "check_availability":
            slots = ", ".join(res.get("available_slots", ["09:30 AM", "03:00 PM"])[:3])
            return (
                f"I checked availability for {res.get('service')} on {res.get('date')}. "
                f"We have openings at {slots}. Would you like me to book one of those times for you?"
            )
        elif tool_name == "book_appointment":
            return (
                f"Your appointment for {res.get('service')} has been booked! "
                f"Your confirmation number is {res.get('booking_id')} for {res.get('scheduled_at')}. "
                f"{res.get('cancellation_policy')}"
            )
        elif tool_name == "query_knowledge_base":
            return f"{res.get('snippet')} Let me know if you would like more information."
        elif tool_name == "execute_webhook":
            return f"The webhook request to {res.get('url')} was dispatched and acknowledged with status {res.get('status_code', 200)}."
        elif tool_name == "transfer_call":
            return (
                f"I am transferring you to our {res.get('department', 'specialist')} department now "
                f"at {res.get('target_number')}. Please hold while you are connected."
            )
        else:
            return f"The tool '{tool_name}' completed successfully with result: {json.dumps(res)}."

    def cancel_active_generation(self) -> bool:
        """Instantly cancel in-flight streaming generation when caller barges in."""
        if self.active_generation_task and not self.active_generation_task.done():
            self.active_generation_task.cancel()
            log.info("Active LLM streaming generation cancelled due to barge-in.")
            return True
        return False

    async def _mock_stream(self, prompt: str) -> AsyncIterable[str]:
        """
        Fast local streaming simulation with realistic conversational pacing (~15ms/token)
        for offline testing, development, and acceptance checks without external API dependencies.
        """
        lower = prompt.lower()
        if re.search(r"\b(what can you do|how can you help|capabilities|help me)\b", lower):
            reply = (
                "I can help you schedule doctor appointments with Dr. Mohan in Cardiology, "
                "check clinic availability, track orders, answer questions, or transfer you to a specialist. "
                "How can I assist you right now?"
            )
        elif re.search(r"\b(hello|hi|hey|good morning|good afternoon)\b", lower) and len(lower.split()) <= 4:
            reply = (
                "Hello there! I am your AI voice assistant. "
                "I am listening and ready to help you with whatever you need."
            )
        elif re.search(r"\b(cardio|cardiology|heart doctor)\b", lower):
            reply = (
                "Dr. Mohan is our Cardiology specialist. "
                "He has appointments available on Friday, September 11th at 09:30 AM and 03:00 PM. "
                "Would you like me to reserve one of those times for you?"
            )
        elif re.search(r"\b(9:30|nine thirty|3:00|three pm|choose)\b", lower):
            slot = "09:30 AM" if ("9:30" in lower or "nine thirty" in lower) else "03:00 PM"
            reply = (
                f"Your appointment with Dr. Mohan for {slot} on Friday, September 11th has been confirmed! "
                "Your booking reference is BK-7842. Free cancellation is available up to 24 hours prior."
            )
        elif "time" in lower and not ("appointment" in lower or "slot" in lower or "doctor" in lower):
            reply = f"The current system time is {time.strftime('%I:%M %p')}. How else can I assist you today?"
        elif "who are you" in lower or "what are you" in lower:
            reply = (
                "I am the real-time AI voice assistant running on LiveKit. "
                "I process your voice using streaming speech-to-text, local Silero VAD, and streaming LLM."
            )
        elif "barge" in lower or "interrupt" in lower:
            reply = (
                "You can interrupt me at any point while I am speaking, "
                "and the barge-in engine will immediately cut off my response and listen to you."
            )
        elif "weather" in lower:
            reply = (
                "The weather looks clear and pleasant today, with a light breeze. "
                "Is there anything specific you would like to know?"
            )
        elif "no" in lower and len(lower.split()) <= 3:
            reply = "Understood. What date or time would you prefer instead?"
        elif "yes" in lower or "okay" in lower or "sure" in lower:
            reply = "Great! I have confirmed your request. Is there anything else I can help you with?"
        else:
            reply = (
                f"I understood: {prompt.strip()}. "
                "I can help you confirm this with Dr. Mohan or adjust the appointment time. "
                "Would you like to proceed with 09:30 AM or 03:00 PM on Friday?"
            )

        # Split into realistic streaming tokens (words + punctuation)
        tokens = re.findall(r"\S+|\s+", reply)
        for tok in tokens:
            await asyncio.sleep(0.018)  # ~18ms per token simulates LLM streaming speed
            yield tok

    async def _openai_stream(self, messages: List[dict]) -> AsyncIterable[str]:
        """Streams tokens directly from OpenAI using the AsyncClient."""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.api_key)
        stream = await client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content

    async def generate_response(
        self,
        user_text: str,
        on_token: Optional[Callable[[str], Any]] = None,
        on_clause: Optional[Callable[[str, bool, int], Any]] = None,
        on_tool_call: Optional[Callable[[str, dict], Any]] = None,
        on_filler: Optional[Callable[[str], Any]] = None,
        on_tool_result: Optional[Callable[[str, dict], Any]] = None,
    ) -> dict:
        """
        Executes a streaming LLM turn:
        - Appends user message to context buffer.
        - Detects mid-call tool invocations (or OpenAI tool calls).
        - Emits filler speech immediately (<100ms) to eliminate dead air.
        - Dispatches tools asynchronously and formats grounded responses.
        - Streams response tokens and clause chunks.
        - Measures TTFT and generation duration.
        - Appends assistant message to context buffer.
        - Handles barge-in cancellation cleanly.
        """
        # Cancel any previous in-flight generation
        self.cancel_active_generation()

        # Add user turn to context
        self.context.add_user_message(user_text)
        messages = self.context.get_messages_for_llm()

        splitter = ClauseBoundarySplitter()
        accumulated_text: List[str] = []
        clauses_emitted: List[str] = []
        tool_calls_executed: List[dict] = []

        start_time = time.perf_counter()
        first_token_time: Optional[float] = None
        interrupted = False

        async def _run():
            nonlocal first_token_time, interrupted
            try:
                detected_tool = self._detect_simulated_tool_call(user_text)
                if self._is_mock and detected_tool:
                    tool_name, tool_args = detected_tool
                    log.info("Triggered mid-call tool: %s(%s)", tool_name, tool_args)
                    if on_tool_call:
                        res = on_tool_call(tool_name, tool_args)
                        if asyncio.iscoroutine(res):
                            await res

                    # 1. Immediate filler speech to keep caller engaged
                    filler = self.tool_registry.filler_engine.get_filler(tool_name, tool_args)
                    if on_filler:
                        res = on_filler(filler)
                        if asyncio.iscoroutine(res):
                            await res

                    # 2. Async tool dispatch
                    tool_res = await self.tool_dispatcher.execute_tool(tool_name, tool_args)
                    if on_tool_result:
                        res = on_tool_result(tool_name, tool_res)
                        if asyncio.iscoroutine(res):
                            await res

                    tool_calls_executed.append({
                        "tool": tool_name,
                        "args": tool_args,
                        "result": tool_res,
                        "filler": filler,
                    })

                    # 3. Grounded reply generation
                    grounded_reply = self._format_grounded_tool_response(tool_name, tool_res)
                    tokens = re.findall(r"\S+|\s+", grounded_reply)
                    for tok in tokens:
                        await asyncio.sleep(0.016)
                        if first_token_time is None:
                            first_token_time = time.perf_counter()

                        accumulated_text.append(tok)
                        if on_token:
                            res = on_token(tok)
                            if asyncio.iscoroutine(res):
                                await res

                        new_clauses = splitter.feed_token(tok)
                        for clause in new_clauses:
                            clauses_emitted.append(clause)
                            if on_clause:
                                res = on_clause(clause, False, len(clauses_emitted))
                                if asyncio.iscoroutine(res):
                                    await res
                else:
                    if self._is_mock:
                        token_stream = self._mock_stream(user_text)
                    else:
                        token_stream = self._openai_stream(messages)

                    async for token in token_stream:
                        if first_token_time is None:
                            first_token_time = time.perf_counter()

                        accumulated_text.append(token)
                        if on_token:
                            res = on_token(token)
                            if asyncio.iscoroutine(res):
                                await res

                        new_clauses = splitter.feed_token(token)
                        for clause in new_clauses:
                            clauses_emitted.append(clause)
                            if on_clause:
                                res = on_clause(clause, False, len(clauses_emitted))
                                if asyncio.iscoroutine(res):
                                    await res

                # Stream ended cleanly: flush remaining buffer
                final_clauses = splitter.flush()
                for clause in final_clauses:
                    clauses_emitted.append(clause)
                    if on_clause:
                        res = on_clause(clause, True, len(clauses_emitted))
                        if asyncio.iscoroutine(res):
                            await res

            except asyncio.CancelledError:
                interrupted = True
                log.info("LLM generation task was interrupted by caller barge-in.")
                raise

        self.active_generation_task = asyncio.create_task(_run())

        try:
            await self.active_generation_task
        except asyncio.CancelledError:
            interrupted = True

        duration = time.perf_counter() - start_time
        ttft_ms = (
            round((first_token_time - start_time) * 1000.0, 2)
            if first_token_time
            else None
        )
        total_text = "".join(accumulated_text).strip()

        # Commit response to context buffer
        self.context.add_assistant_message(total_text, interrupted=interrupted)

        metrics = {
            "text": total_text,
            "ttft_ms": ttft_ms,
            "duration_ms": round(duration * 1000.0, 2),
            "token_count": len(accumulated_text),
            "clause_count": len(clauses_emitted),
            "interrupted": interrupted,
            "clauses": clauses_emitted,
            "model": self.model if not self._is_mock else "mock-simulator",
            "tool_calls": tool_calls_executed,
        }
        return metrics
