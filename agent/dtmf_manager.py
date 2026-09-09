"""
Task 2.3 — DTMF Digit Handling & IVR Navigation Engine.

Components:
1. DTMF Digit Models & RFC 4733 / RFC 2833 Telephony Event Parser.
2. In-band DTMF Audio Tone Detector (Goertzel Algorithm DSP).
3. Multi-digit Keypad Sequence Buffer (PIN, account number, or order ID capture with '#' terminator).
4. IVR Phone Tree Navigation Engine (Menu prompts, branching nodes, timeout fallback, retry handling).
5. Voice Agent Integration (Keypad events drive conversational routing, transfers, and tool execution).
"""

import asyncio
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("dtmf-manager")

# Standard DTMF Frequency Grid (Hz)
# Row frequencies (low group)
DTMF_LOW_FREQS = [697, 770, 852, 941]
# Column frequencies (high group)
DTMF_HIGH_FREQS = [1209, 1336, 1477, 1633]

DTMF_MATRIX: Dict[Tuple[int, int], str] = {
    (697, 1209): "1", (697, 1336): "2", (697, 1477): "3", (697, 1633): "A",
    (770, 1209): "4", (770, 1336): "5", (770, 1477): "6", (770, 1633): "B",
    (852, 1209): "7", (852, 1336): "8", (852, 1477): "9", (852, 1633): "C",
    (941, 1209): "*", (941, 1336): "0", (941, 1477): "#", (941, 1633): "D",
}

VALID_DTMF_DIGITS = set("0123456789*#ABCD")


class DTMFActionType(str, Enum):
    NAVIGATE = "navigate"      # Move to a submenu node
    TRANSFER = "transfer"      # Transfer to another department / human agent
    TOOL = "tool"              # Execute a backend mid-call tool
    REPEAT = "repeat"          # Re-announce current menu prompt
    HANGUP = "hangup"          # Terminate call leg


@dataclass
class DTMFEvent:
    digit: str
    duration_ms: int = 160
    volume_dbov: float = -10.0
    source_participant: str = "caller"
    timestamp: float = field(default_factory=time.time)


@dataclass
class IVRMenuOption:
    digit: str
    label: str
    action_type: DTMFActionType
    target_node_id: Optional[str] = None
    target_department: Optional[str] = None
    transfer_number: Optional[str] = None
    tool_name: Optional[str] = None
    tool_args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IVRMenuNode:
    node_id: str
    name: str
    prompt: str
    options: Dict[str, IVRMenuOption] = field(default_factory=dict)
    timeout_seconds: float = 6.0
    max_retries: int = 2
    fallback_node_id: Optional[str] = None


class GoertzelDetector:
    """
    Goertzel Algorithm DSP implementation for detecting DTMF dual-tone frequencies
    in raw PCM audio samples (8kHz, 16kHz, 24kHz).
    """

    @staticmethod
    def _goertzel_mag(samples: List[float], target_freq: float, sample_rate: int) -> float:
        n = len(samples)
        if n == 0:
            return 0.0
        k = int(0.5 + (n * target_freq / sample_rate))
        omega = (2.0 * math.pi * k) / n
        coeff = 2.0 * math.cos(omega)

        q1 = 0.0
        q2 = 0.0
        for sample in samples:
            q0 = coeff * q1 - q2 + sample
            q2 = q1
            q1 = q0

        magnitude_sq = q1 * q1 + q2 * q2 - q1 * q2 * coeff
        return math.sqrt(max(0.0, magnitude_sq)) / (n / 2.0)

    @classmethod
    def detect_tone(cls, samples: List[float], sample_rate: int = 8000, threshold: float = 0.15) -> Optional[str]:
        """
        Analyzes audio buffer and returns the detected DTMF character if low and high
        tone frequencies both exceed threshold.
        """
        if not samples or len(samples) < 160:
            return None

        # Find peak low frequency
        best_low, best_low_mag = None, 0.0
        for f in DTMF_LOW_FREQS:
            mag = cls._goertzel_mag(samples, f, sample_rate)
            if mag > best_low_mag:
                best_low, best_low_mag = f, mag

        # Find peak high frequency
        best_high, best_high_mag = None, 0.0
        for f in DTMF_HIGH_FREQS:
            mag = cls._goertzel_mag(samples, f, sample_rate)
            if mag > best_high_mag:
                best_high, best_high_mag = f, mag

        if best_low_mag >= threshold and best_high_mag >= threshold:
            return DTMF_MATRIX.get((best_low, best_high))
        return None

    @classmethod
    def generate_tone(cls, digit: str, duration_ms: int = 160, sample_rate: int = 8000) -> List[float]:
        """Generates dual-frequency sinusoidal PCM float samples [-1.0, 1.0] for a DTMF digit."""
        digit = digit.upper()
        pair = None
        for freqs, char in DTMF_MATRIX.items():
            if char == digit:
                pair = freqs
                break
        if not pair:
            return [0.0] * int(sample_rate * (duration_ms / 1000.0))

        f_low, f_high = pair
        total_samples = int(sample_rate * (duration_ms / 1000.0))
        result = []
        for i in range(total_samples):
            t = i / float(sample_rate)
            val = 0.5 * math.sin(2.0 * math.pi * f_low * t) + 0.5 * math.sin(2.0 * math.pi * f_high * t)
            result.append(val)
        return result


class DTMFDigitBuffer:
    """
    Accumulates keypad digit entries for multi-digit input sequences
    (e.g., account numbers, 4-digit PINs, or order IDs ending with '#').
    """

    def __init__(self, max_length: int = 32, terminator: str = "#"):
        self.max_length = max_length
        self.terminator = terminator
        self._digits: List[str] = []
        self._last_digit_time: float = 0.0

    def push(self, digit: str) -> bool:
        """Adds a digit. Returns True if terminator reached."""
        clean = digit.strip().upper()
        if not clean or clean not in VALID_DTMF_DIGITS:
            return False

        self._last_digit_time = time.time()
        if clean == self.terminator:
            return True

        if len(self._digits) < self.max_length:
            self._digits.append(clean)

        return False

    def backspace(self) -> Optional[str]:
        if self._digits:
            return self._digits.pop()
        return None

    def get_value(self) -> str:
        return "".join(self._digits)

    def clear(self) -> None:
        self._digits.clear()
        self._last_digit_time = 0.0

    def __len__(self) -> int:
        return len(self._digits)


class DTMFManager:
    """
    Manages DTMF digit processing, IVR menu phone trees, and state routing.
    """

    def __init__(self):
        self._menus: Dict[str, IVRMenuNode] = {}
        self._active_nodes: Dict[str, str] = {}  # call_id -> current node_id
        self._buffers: Dict[str, DTMFDigitBuffer] = {}  # call_id -> digit buffer
        self._retry_counts: Dict[str, int] = {}  # call_id -> current retry count
        self._history: Dict[str, List[DTMFEvent]] = {}  # call_id -> digit history
        self._init_default_menus()

    def _init_default_menus(self):
        """Initializes default IVR phone tree with customer service routing."""
        # 1. Main Welcome Menu
        main_menu = IVRMenuNode(
            node_id="main",
            name="Main Customer Menu",
            prompt="Thanks for calling Northgate Support. Press 1 for Sales, 2 for Technical Support, 3 for Billing, or 9 for a live operator.",
            timeout_seconds=6.0,
            max_retries=2,
            fallback_node_id="operator",
        )
        main_menu.options = {
            "1": IVRMenuOption(
                digit="1",
                label="Sales & New Accounts",
                action_type=DTMFActionType.NAVIGATE,
                target_node_id="sales",
            ),
            "2": IVRMenuOption(
                digit="2",
                label="Technical Support",
                action_type=DTMFActionType.TRANSFER,
                target_department="technical support",
                transfer_number="+18885550142",
            ),
            "3": IVRMenuOption(
                digit="3",
                label="Billing & Accounts",
                action_type=DTMFActionType.TRANSFER,
                target_department="billing",
                transfer_number="+18005550199",
            ),
            "9": IVRMenuOption(
                digit="9",
                label="Operator / Human Specialist",
                action_type=DTMFActionType.TRANSFER,
                target_department="customer support",
                transfer_number="+18005550100",
            ),
            "*": IVRMenuOption(
                digit="*",
                label="Repeat Menu",
                action_type=DTMFActionType.REPEAT,
            ),
        }
        self.register_menu(main_menu)

        # 2. Sales Submenu
        sales_menu = IVRMenuNode(
            node_id="sales",
            name="Sales Department",
            prompt="Sales department. Press 1 for Enterprise Solutions, 2 for Small Business, or 0 to return to the main menu.",
            timeout_seconds=6.0,
            fallback_node_id="main",
        )
        sales_menu.options = {
            "1": IVRMenuOption(
                digit="1",
                label="Enterprise Sales",
                action_type=DTMFActionType.TRANSFER,
                target_department="enterprise sales",
                transfer_number="+18885550188",
            ),
            "2": IVRMenuOption(
                digit="2",
                label="Small Business Sales",
                action_type=DTMFActionType.TRANSFER,
                target_department="smb sales",
                transfer_number="+18885550177",
            ),
            "0": IVRMenuOption(
                digit="0",
                label="Main Menu",
                action_type=DTMFActionType.NAVIGATE,
                target_node_id="main",
            ),
        }
        self.register_menu(sales_menu)

    def register_menu(self, node: IVRMenuNode) -> None:
        self._menus[node.node_id] = node
        log.info("Registered IVR menu node: %s (%s options)", node.node_id, len(node.options))

    def get_or_create_buffer(self, call_id: str) -> DTMFDigitBuffer:
        if call_id not in self._buffers:
            self._buffers[call_id] = DTMFDigitBuffer()
        return self._buffers[call_id]

    def get_active_node(self, call_id: str) -> IVRMenuNode:
        node_id = self._active_nodes.get(call_id, "main")
        return self._menus.get(node_id) or self._menus["main"]

    def reset_call(self, call_id: str) -> None:
        self._active_nodes[call_id] = "main"
        self._retry_counts[call_id] = 0
        if call_id in self._buffers:
            self._buffers[call_id].clear()
        self._history[call_id] = []
        log.info("Reset IVR session for call: %s", call_id)

    def process_dtmf_digit(
        self,
        call_id: str,
        digit: str,
        duration_ms: int = 160,
        source_participant: str = "caller",
    ) -> dict:
        """
        Handles incoming DTMF digit press:
        1. Records event in session history.
        2. Updates the multi-digit sequence buffer.
        3. Evaluates active IVR menu tree routing.
        """
        clean_digit = digit.strip().upper()
        if clean_digit not in VALID_DTMF_DIGITS:
            return {
                "status": "invalid_digit",
                "digit": digit,
                "error": f"Invalid DTMF digit '{digit}'",
            }

        # 1. Record event
        evt = DTMFEvent(
            digit=clean_digit,
            duration_ms=duration_ms,
            source_participant=source_participant,
        )
        if call_id not in self._history:
            self._history[call_id] = []
        self._history[call_id].append(evt)

        # 2. Add to buffer
        buf = self.get_or_create_buffer(call_id)
        is_terminated = buf.push(clean_digit)
        buffered_digits = buf.get_value()

        # 3. Evaluate IVR menu option
        node = self.get_active_node(call_id)
        option = node.options.get(clean_digit)

        if option:
            self._retry_counts[call_id] = 0
            log.info("IVR matched option: '%s' -> %s (%s)", clean_digit, option.label, option.action_type.value)

            if option.action_type == DTMFActionType.NAVIGATE:
                target_node = self._menus.get(option.target_node_id or "main")
                if target_node:
                    self._active_nodes[call_id] = target_node.node_id
                    return {
                        "status": "navigated",
                        "digit": clean_digit,
                        "action": "navigate",
                        "node_id": target_node.node_id,
                        "node_name": target_node.name,
                        "prompt": target_node.prompt,
                        "buffered_digits": buffered_digits,
                    }

            elif option.action_type == DTMFActionType.TRANSFER:
                return {
                    "status": "transfer_triggered",
                    "digit": clean_digit,
                    "action": "transfer",
                    "department": option.target_department,
                    "transfer_number": option.transfer_number,
                    "prompt": f"Connecting you to {option.target_department or 'our specialist'} now, please hold.",
                    "buffered_digits": buffered_digits,
                }

            elif option.action_type == DTMFActionType.REPEAT:
                return {
                    "status": "repeat",
                    "digit": clean_digit,
                    "action": "repeat",
                    "node_id": node.node_id,
                    "prompt": node.prompt,
                    "buffered_digits": buffered_digits,
                }

            elif option.action_type == DTMFActionType.TOOL:
                return {
                    "status": "tool_triggered",
                    "digit": clean_digit,
                    "action": "tool",
                    "tool_name": option.tool_name,
                    "tool_args": option.tool_args,
                    "buffered_digits": buffered_digits,
                }

        # Digit not in current menu options
        retries = self._retry_counts.get(call_id, 0) + 1
        self._retry_counts[call_id] = retries
        log.warning("IVR unmatched digit: '%s' on node '%s' (retry %d/%d)", clean_digit, node.node_id, retries, node.max_retries)

        if retries >= node.max_retries and node.fallback_node_id:
            fallback = self._menus.get(node.fallback_node_id) or self._menus["main"]
            self._active_nodes[call_id] = fallback.node_id
            self._retry_counts[call_id] = 0
            return {
                "status": "max_retries_fallback",
                "digit": clean_digit,
                "action": "fallback",
                "node_id": fallback.node_id,
                "prompt": f"Sorry, I did not recognize that option. {fallback.prompt}",
                "buffered_digits": buffered_digits,
            }

        return {
            "status": "unmatched_digit",
            "digit": clean_digit,
            "action": "retry",
            "node_id": node.node_id,
            "prompt": f"Invalid option '{clean_digit}'. {node.prompt}",
            "buffered_digits": buffered_digits,
            "is_terminated": is_terminated,
        }

    def get_call_state(self, call_id: str) -> dict:
        node = self.get_active_node(call_id)
        buf = self.get_or_create_buffer(call_id)
        history = self._history.get(call_id, [])
        return {
            "call_id": call_id,
            "active_node": {
                "node_id": node.node_id,
                "name": node.name,
                "prompt": node.prompt,
                "options": {k: asdict(v) for k, v in node.options.items()},
            },
            "buffered_digits": buf.get_value(),
            "history_count": len(history),
            "last_digits": [asdict(e) for e in history[-8:]],
        }

    def list_menus(self) -> List[dict]:
        return [
            {
                "node_id": m.node_id,
                "name": m.name,
                "prompt": m.prompt,
                "options": {k: asdict(v) for k, v in m.options.items()},
            }
            for m in self._menus.values()
        ]


# Global singleton instance
dtmf_manager = DTMFManager()
