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
import concurrent.futures
import functools
import inspect
import ipaddress
import json
import logging
import os
import random
import re
import socket
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("tool-manager")

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
CUSTOM_TOOLS_FILE = os.path.join(CONFIG_DIR, "custom_tools.json")

_TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="tool-exec")


def _storage_sync(path: str) -> None:
    try:
        from agent import storage
        storage.sync_file(path)
    except Exception as e:
        log.debug("Storage sync skipped for %s: %s", path, e)


def make_http_tool_handler(endpoint: str, method: str, timeout: float):
    async def custom_handler(**kwargs):
        if endpoint:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                if method.upper() == "POST":
                    async with session.post(endpoint, json=kwargs, timeout=timeout) as resp:
                        return {"status_code": resp.status, "body": await resp.text()}
                else:
                    async with session.get(endpoint, params=kwargs, timeout=timeout) as resp:
                        return {"status_code": resp.status, "body": await resp.text()}
        else:
            return {"status": "executed", "echo": kwargs}
    return custom_handler


@dataclass
class ToolDefinition:
    """Represents a callable tool with JSON Schema parameters and execution metadata."""
    name: str
    description: str
    parameters: dict  # JSON Schema: {"type": "object", "properties": {...}, "required": [...]}
    handler: Callable[..., Any]
    filler_phrases: List[str] = field(default_factory=list)
    timeout: float = 3.0
    metadata: Dict[str, Any] = field(default_factory=dict)

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
        safe_args = {}
        if arguments:
            for k, v in arguments.items():
                if v is None:
                    safe_args[k] = "support" if k == "department" else ""
                else:
                    safe_args[k] = str(v)
        try:
            formatted = template.format(**safe_args)
        except Exception:
            formatted = template
        # Clean up any lingering unfilled {placeholder}
        formatted = re.sub(r"\{[a-zA-Z0-9_]+\}\s*", "", formatted)
        return re.sub(r"\s+", " ", formatted).strip()


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
            if inspect.iscoroutinefunction(handler):
                result = await asyncio.wait_for(handler(**arguments), timeout=exec_timeout)
            else:
                loop = asyncio.get_running_loop()
                fut = loop.run_in_executor(_TOOL_EXECUTOR, functools.partial(handler, **arguments))
                result = await asyncio.wait_for(fut, timeout=exec_timeout)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=exec_timeout)

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
    _custom_tools: Dict[str, ToolDefinition] = {}

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self.filler_engine = FillerSpeechEngine()
        self._call_id: Optional[str] = None
        self._room_name: Optional[str] = None
        self._participant_identity: Optional[str] = None
        self._register_default_tools()
        self._load_custom_tools()
        for custom_tool in self._custom_tools.values():
            self.register(custom_tool, persist=False)

    def _load_custom_tools(self) -> None:
        if not os.path.isfile(CUSTOM_TOOLS_FILE):
            return
        try:
            with open(CUSTOM_TOOLS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("tools", []):
                try:
                    name = item["name"]
                    endpoint = item.get("endpoint_url", "")
                    method = item.get("method", "POST")
                    timeout = float(item.get("timeout", 3.0))
                    handler = make_http_tool_handler(endpoint, method, timeout)
                    tool = ToolDefinition(
                        name=name,
                        description=item.get("description", ""),
                        parameters=item.get("parameters") or {"type": "object", "properties": {}},
                        handler=handler,
                        filler_phrases=item.get("filler_phrases") or [],
                        timeout=timeout,
                        metadata=item.get("metadata") or {"endpoint_url": endpoint, "method": method},
                    )
                    self.register(tool, persist=False)
                    ToolRegistry._custom_tools[name] = tool
                except Exception as ex:
                    log.warning("Skipping malformed custom tool: %s", ex)
        except Exception as e:
            log.warning("Failed to load custom tools from disk: %s", e)

    def _save_custom_tools(self) -> None:
        try:
            os.makedirs(os.path.dirname(CUSTOM_TOOLS_FILE), exist_ok=True)
            tool_list = []
            for t in ToolRegistry._custom_tools.values():
                meta = getattr(t, "metadata", {}) or {}
                tool_list.append({
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                    "timeout": t.timeout,
                    "filler_phrases": t.filler_phrases,
                    "endpoint_url": meta.get("endpoint_url", ""),
                    "method": meta.get("method", "POST"),
                    "metadata": meta,
                })
            with open(CUSTOM_TOOLS_FILE, "w", encoding="utf-8") as f:
                json.dump({"tools": tool_list, "updated_at": time.time()}, f, indent=2)
            _storage_sync(CUSTOM_TOOLS_FILE)
        except Exception as e:
            log.warning("Failed to save custom tools: %s", e)

    def set_call_context(self, call_id: str, room_name: str, participant_identity: str) -> None:
        """Sets the active call context so tools know the target room and caller participant identity."""
        self._call_id = call_id
        self._room_name = room_name
        self._participant_identity = participant_identity
        try:
            from agent.transfer_manager import transfer_manager
            transfer_manager.register_call_context(call_id, room_name, participant_identity)
        except Exception as e:
            log.debug("Could not register call context with transfer_manager: %s", e)

    def get_call_context(self) -> Tuple[str, str, str]:
        """Returns (call_id, room_name, participant_identity)."""
        if self._room_name and self._participant_identity:
            return (self._call_id or self._room_name, self._room_name, self._participant_identity)
        try:
            from agent.transfer_manager import transfer_manager
            room, part = transfer_manager.get_call_context(self._call_id)
            return (self._call_id or room, room, part)
        except Exception:
            return (self._call_id or "default-room", self._room_name or "default-room", self._participant_identity or "caller")

    def register(self, tool: ToolDefinition, persist: bool = False) -> None:
        """Registers a tool definition and its filler phrases."""
        self._tools[tool.name] = tool
        if persist:
            ToolRegistry._custom_tools[tool.name] = tool
            self._save_custom_tools()
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

        # 5. Execute Webhook with strict SSRF protection
        def _is_safe_destination(url: str) -> Tuple[bool, str]:
            try:
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme not in ("http", "https"):
                    return False, f"Unsupported scheme '{parsed.scheme}'"
                hostname = parsed.hostname
                if not hostname:
                    return False, "Missing URL hostname"
                # Resolve host IPs and verify none fall into restricted ranges
                addr_info = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
                for family, socktype, proto, canonname, sockaddr in addr_info:
                    ip_str = sockaddr[0]
                    ip = ipaddress.ip_address(ip_str)
                    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                        return False, f"Access to private/internal network IP '{ip_str}' is prohibited"
                return True, ""
            except Exception as e:
                return False, f"Could not validate destination host: {e}"

        async def execute_webhook(
            endpoint_url: str,
            method: str = "POST",
            payload: Optional[dict] = None,
        ) -> dict:
            if endpoint_url.startswith("http://") or endpoint_url.startswith("https://"):
                safe, reason = _is_safe_destination(endpoint_url)
                if not safe:
                    log.warning("SSRF check rejected webhook request to %s: %s", endpoint_url, reason)
                    return {"status_code": 403, "error": f"SSRF blocked: {reason}", "url": endpoint_url}
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
            from agent.transfer_manager import transfer_manager, TransferMode, TransferStatus
            norm_type = TransferMode.WARM if str(transfer_type).lower() == "warm" else TransferMode.BLIND
            call_id, room_name, participant_id = self.get_call_context()
            effective_call_id = room_name or call_id
            if norm_type == TransferMode.WARM:
                rec = await transfer_manager.initiate_warm_transfer(
                    call_id=effective_call_id,
                    target_number=destination,
                    source_participant=participant_id,
                    department=department,
                    reason=reason,
                    caller_inquiry=reason or "Customer inquiry",
                )
            else:
                rec = await transfer_manager.initiate_blind_transfer(
                    call_id=effective_call_id,
                    target_number=destination,
                    source_participant=participant_id,
                    department=department,
                    reason=reason,
                )
            is_success = rec.status != TransferStatus.FAILED
            msg = (
                f"Transfer to {department or destination} ({rec.mode.value}) initiated successfully."
                if is_success
                else f"Transfer to {department or destination} failed: {rec.failure_reason or 'unknown error'}."
            )
            return {
                "transfer_id": rec.transfer_id,
                "status": rec.status.value,
                "target_number": rec.target_number,
                "mode": rec.mode.value,
                "department": department or "specialist",
                "message": msg,
                "error": rec.failure_reason if not is_success else None,
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
