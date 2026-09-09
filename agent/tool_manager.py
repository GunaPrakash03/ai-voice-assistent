"""
Task 1.6 — Mid-Call Function Calling & Retrieval Tools.

Components:
1. ToolRegistry: JSON Schema tool parser and registry for callable tools.
2. AsyncToolDispatcher: Non-blocking async execution engine with timeout protection and error recovery.
3. FillerSpeechEngine: Contextual filler phrases ("Let me check that for you...") preventing dead air.
4. Built-in business tools:
   - check_availability: Calendar slot lookup.
   - book_appointment: Reservation booking engine.
   - lookup_order: Real-time order tracking and status lookup.
   - query_knowledge_base: Knowledge retrieval for policies, pricing, and FAQs.
   - execute_webhook: Async HTTP webhook dispatcher.
"""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("tool-manager")


@dataclass
class ToolDefinition:
    """Represents a callable tool with JSON Schema parameters and execution metadata."""
    name: str
    description: str
    parameters: dict  # JSON Schema: {"type": "object", "properties": {...}, "required": [...]}
    handler: Callable[..., Any]
    filler_phrases: List[str] = field(default_factory=list)
    timeout: float = 3.0

    def to_json_schema(self) -> dict:
        """Returns standard OpenAI / LiveKit compatible tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class FillerSpeechEngine:
    """
    Generates dynamic, natural conversational filler speech phrases
    to keep callers engaged during mid-call tool execution (>300ms latency).
    """

    DEFAULT_FILLERS = [
        "Let me check that for you right now...",
        "One moment while I look that up...",
        "Checking our system for those details...",
        "Just a second, pulling that information up for you...",
    ]

    def __init__(self, custom_fillers: Optional[Dict[str, List[str]]] = None):
        self._tool_fillers: Dict[str, List[str]] = custom_fillers or {}

    def register_tool_fillers(self, tool_name: str, phrases: List[str]) -> None:
        self._tool_fillers[tool_name] = phrases

    def get_filler(self, tool_name: str, arguments: Optional[dict] = None) -> str:
        """Returns a natural contextual filler phrase formatted with arguments if possible."""
        phrases = self._tool_fillers.get(tool_name) or self.DEFAULT_FILLERS
        template = random.choice(phrases)
        if arguments:
            try:
                # Safe formatting for templates with placeholders like {order_id} or {date}
                return template.format(**arguments)
            except Exception:
                return template
        return template


class AsyncToolDispatcher:
    """
    Dispatches tool calls asynchronously without blocking the media/audio loop.
    Enforces timeout limits, handles errors, and records execution metrics.
    """

    def __init__(self, registry: "ToolRegistry"):
        self.registry = registry

    async def execute_tool(
        self,
        name: str,
        arguments: dict,
        timeout: Optional[float] = None,
    ) -> dict:
        """
        Executes a registered tool by name with arguments.
        Returns a structured result dict with latency telemetry.
        """
        tool = self.registry.get_tool(name)
        if not tool:
            return {
                "tool": name,
                "status": "error",
                "result": None,
                "error": f"Tool '{name}' is not registered.",
                "duration_ms": 0.0,
            }

        exec_timeout = timeout if timeout is not None else tool.timeout
        t0 = time.perf_counter()

        try:
            handler = tool.handler
            if asyncio.iscoroutinefunction(handler):
                coro = handler(**arguments)
            else:
                coro = asyncio.to_thread(handler, **arguments)

            result = await asyncio.wait_for(coro, timeout=exec_timeout)
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            log.info("Tool '%s' executed successfully in %.2fms", name, duration_ms)
            return {
                "tool": name,
                "arguments": arguments,
                "status": "success",
                "result": result,
                "error": None,
                "duration_ms": duration_ms,
            }

        except asyncio.TimeoutError:
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            log.warning("Tool '%s' timed out after %.2fms (limit: %.1fs)", name, duration_ms, exec_timeout)
            return {
                "tool": name,
                "arguments": arguments,
                "status": "timeout",
                "result": None,
                "error": f"Tool execution timed out after {exec_timeout} seconds.",
                "duration_ms": duration_ms,
            }

        except Exception as e:
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            log.error("Tool '%s' failed with error: %s", name, e, exc_info=True)
            return {
                "tool": name,
                "arguments": arguments,
                "status": "error",
                "result": None,
                "error": str(e),
                "duration_ms": duration_ms,
            }


class ToolRegistry:
    """
    Central registry for callable tools with JSON Schema validation
    and default business tools for voice assistant workflows.
    """

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self.filler_engine = FillerSpeechEngine()
        self._register_default_tools()

    def register(self, tool: ToolDefinition) -> None:
        """Registers a tool definition and its filler phrases."""
        self._tools[tool.name] = tool
        if tool.filler_phrases:
            self.filler_engine.register_tool_fillers(tool.name, tool.filler_phrases)
        log.info("Registered function tool: '%s'", tool.name)

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def list_tools(self) -> List[ToolDefinition]:
        return list(self._tools.values())

    def get_schemas(self) -> List[dict]:
        """Returns JSON schema definitions for all registered tools."""
        return [t.to_json_schema() for t in self._tools.values()]

    def _register_default_tools(self) -> None:
        """Registers standard business tools for common voice agent tasks."""

        # 1. Check Availability
        async def check_availability(service_type: str = "general", date: str = "today") -> dict:
            await asyncio.sleep(0.08)  # simulate brief calendar query
            slots = ["09:30 AM", "11:00 AM", "02:15 PM", "04:00 PM"]
            return {
                "service": service_type,
                "date": date,
                "available_slots": slots,
                "timezone": "EST",
                "total_slots": len(slots),
            }

        self.register(
            ToolDefinition(
                name="check_availability",
                description="Check available appointment time slots for a specified service type and date.",
                parameters={
                    "type": "object",
                    "properties": {
                        "service_type": {
                            "type": "string",
                            "description": "The category or type of service (e.g. consultation, inspection, support).",
                        },
                        "date": {
                            "type": "string",
                            "description": "The requested date, e.g. 'today', 'tomorrow', or 'YYYY-MM-DD'.",
                        },
                    },
                    "required": ["service_type"],
                },
                handler=check_availability,
                filler_phrases=[
                    "Let me check our calendar for available {service_type} times...",
                    "Looking into the schedule for {service_type} right now...",
                    "One second while I pull up open appointment slots...",
                ],
                timeout=2.5,
            )
        )

        # 2. Book Appointment
        async def book_appointment(
            name: str,
            phone: str,
            date: str,
            time_slot: str,
            service: str = "general",
        ) -> dict:
            await asyncio.sleep(0.12)  # simulate reservation commit
            booking_id = f"BK-{random.randint(1000, 9999)}"
            return {
                "booking_id": booking_id,
                "status": "confirmed",
                "customer_name": name,
                "phone": phone,
                "service": service,
                "scheduled_at": f"{date} at {time_slot}",
                "cancellation_policy": "Free cancellation up to 24 hours prior.",
            }

        self.register(
            ToolDefinition(
                name="book_appointment",
                description="Book a confirmed appointment reservation for a customer.",
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Customer full name"},
                        "phone": {"type": "string", "description": "Customer contact phone number"},
                        "date": {"type": "string", "description": "Appointment date"},
                        "time_slot": {"type": "string", "description": "Appointment time slot, e.g. '11:00 AM'"},
                        "service": {"type": "string", "description": "Service name or type"},
                    },
                    "required": ["name", "date", "time_slot"],
                },
                handler=book_appointment,
                filler_phrases=[
                    "Booking that appointment slot for you right now...",
                    "Securing your reservation in our booking system...",
                    "One moment while I confirm your appointment details...",
                ],
                timeout=3.0,
            )
        )

        # 3. Lookup Order
        async def lookup_order(order_id: str) -> dict:
            await asyncio.sleep(0.09)  # simulate ERP/OMS lookup
            clean_id = str(order_id).strip().upper()
            return {
                "order_id": clean_id,
                "status": "Out for delivery",
                "carrier": "FedEx Express",
                "tracking_number": f"FX-992817{clean_id[-3:] if len(clean_id)>=3 else '001'}",
                "estimated_delivery": "Today by 5:00 PM",
                "destination": "Springfield, IL",
                "items_count": 2,
            }

        self.register(
            ToolDefinition(
                name="lookup_order",
                description="Look up tracking details, shipping carrier, and delivery status for an order ID.",
                parameters={
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string", "description": "The order reference ID, e.g. 'ORD-1042' or '1042'"},
                    },
                    "required": ["order_id"],
                },
                handler=lookup_order,
                filler_phrases=[
                    "Looking up the latest tracking details for order {order_id}...",
                    "Checking the status of your order in our fulfillment system...",
                    "One moment while I pull up those shipping records...",
                ],
                timeout=2.5,
            )
        )

        # 4. Query Knowledge Base
        async def query_knowledge_base(query: str, category: Optional[str] = None) -> dict:
            await asyncio.sleep(0.07)  # simulate vector retrieval
            q = query.lower()
            if "hour" in q or "open" in q or "time" in q:
                snippet = "Our business hours are Monday through Friday 8:00 AM to 8:00 PM EST, and Saturday 9:00 AM to 5:00 PM EST."
            elif "return" in q or "refund" in q:
                snippet = "We offer a 30-day hassle-free return policy. Full refunds are processed to the original payment method within 3 business days."
            elif "price" in q or "cost" in q or "tier" in q:
                snippet = "Our pricing plans start at $49/month for Starter and $199/month for Professional with 24/7 dedicated voice support."
            else:
                snippet = f"General policy for '{query}': All services include 99.9% uptime SLA and real-time WebRTC voice agent monitoring."

            return {
                "query": query,
                "category": category or "general",
                "snippet": snippet,
                "confidence_score": 0.94,
            }

        self.register(
            ToolDefinition(
                name="query_knowledge_base",
                description="Search documentation and knowledge base for FAQs, return policies, hours, and pricing.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural language search query or question"},
                        "category": {"type": "string", "description": "Optional category filter (e.g. policies, hours, pricing)"},
                    },
                    "required": ["query"],
                },
                handler=query_knowledge_base,
                filler_phrases=[
                    "Let me check our knowledge base for that information...",
                    "Searching our policy documentation right now...",
                    "One second while I find the exact answer for you...",
                ],
                timeout=2.5,
            )
        )

        # 5. Execute Webhook
        async def execute_webhook(
            endpoint_url: str,
            method: str = "POST",
            payload: Optional[dict] = None,
        ) -> dict:
            # Safe simulated or real HTTP dispatcher
            t_exec = time.perf_counter()
            if endpoint_url.startswith("http://") or endpoint_url.startswith("https://"):
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        if method.upper() == "POST":
                            async with session.post(endpoint_url, json=payload or {}, timeout=2.0) as resp:
                                status = resp.status
                                data = await resp.text()
                        else:
                            async with session.get(endpoint_url, timeout=2.0) as resp:
                                status = resp.status
                                data = await resp.text()
                    return {"status_code": status, "body": data[:200], "url": endpoint_url}
                except Exception as ex:
                    log.warning("Webhook dispatch error to %s: %s", endpoint_url, ex)
                    return {"status_code": 500, "error": str(ex), "url": endpoint_url}
            else:
                # Simulated webhook response for testing
                await asyncio.sleep(0.06)
                return {
                    "status_code": 200,
                    "url": endpoint_url,
                    "acknowledged": True,
                    "dispatched_at": time.time(),
                    "payload_echo": payload or {},
                }

        self.register(
            ToolDefinition(
                name="execute_webhook",
                description="Dispatch an asynchronous HTTP webhook to an external business API or endpoint.",
                parameters={
                    "type": "object",
                    "properties": {
                        "endpoint_url": {"type": "string", "description": "The destination URL or webhook identifier"},
                        "method": {"type": "string", "enum": ["GET", "POST"], "description": "HTTP method"},
                        "payload": {"type": "object", "description": "JSON payload to transmit"},
                    },
                    "required": ["endpoint_url"],
                },
                handler=execute_webhook,
                filler_phrases=[
                    "Dispatching request to external system, one moment...",
                    "Connecting to the external service now...",
                ],
                timeout=3.5,
            )
        )

        # 6. Transfer Call (Task 2.2)
        async def transfer_call(
            destination: str,
            transfer_type: str = "blind",
            department: Optional[str] = None,
            reason: Optional[str] = None,
        ) -> dict:
            from agent.transfer_manager import transfer_manager, TransferMode
            norm_type = TransferMode.WARM if str(transfer_type).lower() == "warm" else TransferMode.BLIND
            if norm_type == TransferMode.WARM:
                rec = await transfer_manager.initiate_warm_transfer(
                    call_id=f"call-{int(time.time())}",
                    target_number=destination,
                    department=department,
                    reason=reason,
                    caller_inquiry=reason or "Customer inquiry",
                )
            else:
                rec = await transfer_manager.initiate_blind_transfer(
                    call_id=f"call-{int(time.time())}",
                    target_number=destination,
                    department=department,
                    reason=reason,
                )
            return {
                "transfer_id": rec.transfer_id,
                "status": rec.status.value,
                "target_number": rec.target_number,
                "mode": rec.mode.value,
                "department": department or "specialist",
                "message": f"Transfer to {department or destination} ({rec.mode.value}) initiated successfully.",
            }

        self.register(
            ToolDefinition(
                name="transfer_call",
                description="Transfer the current live caller to another telephone number, department, or human specialist.",
                parameters={
                    "type": "object",
                    "properties": {
                        "destination": {"type": "string", "description": "Target phone number or extension (E.164 format, e.g. +18885550142)"},
                        "transfer_type": {"type": "string", "enum": ["blind", "warm"], "description": "Transfer mode: blind (immediate handoff) or warm (attended with briefing)"},
                        "department": {"type": "string", "description": "Target department name, e.g. 'billing', 'technical support', 'sales'"},
                        "reason": {"type": "string", "description": "Reason for the transfer to provide in handoff briefing"},
                    },
                    "required": ["destination"],
                },
                handler=transfer_call,
                filler_phrases=[
                    "Transferring you to our {department} specialist right now, please stay on the line...",
                    "I am connecting you directly with a human representative, one moment please...",
                    "Placing you on a brief hold while I conference in our specialist...",
                ],
                timeout=4.0,
            )
        )
