"""Task 3.2 — Summary & Sentiment Analyzer.

Provides zero-shot multi-dimensional sentiment classification, frustration/escalation
detection, sentiment progression trajectory tracking, and executive narrative summary
generation with actionable follow-ups.
"""

import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("voice-agent.sentiment")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)


class SentimentPolarity(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    PENDING_FOLLOWUP = "pending_followup"
    ESCALATED = "escalated"
    INQUIRY = "inquiry"


# Lexicon of positive, negative, and frustration markers
POSITIVE_LEXICON = {
    "great": 1.5, "excellent": 2.0, "wonderful": 2.0, "perfect": 1.8,
    "thank": 1.2, "thanks": 1.2, "appreciate": 1.5, "helpful": 1.4,
    "good": 1.0, "awesome": 1.8, "glad": 1.2, "happy": 1.4,
    "cleared": 1.0, "confirmed": 1.2, "resolved": 1.5, "satisfied": 1.6,
    "pleasure": 1.4, "fantastic": 1.8, "terrific": 1.8, "sure": 0.6,
    "understand": 0.8, "welcome": 1.0, "prompt": 1.0, "efficient": 1.5
}

NEGATIVE_LEXICON = {
    "bad": -1.2, "terrible": -2.0, "horrible": -2.2, "awful": -2.0,
    "unacceptable": -2.2, "angry": -1.8, "upset": -1.6, "frustrated": -2.0,
    "annoyed": -1.5, "disappointed": -1.8, "wrong": -1.2, "error": -1.2,
    "issue": -1.0, "problem": -1.2, "broken": -1.5, "fail": -1.5,
    "failed": -1.5, "refuse": -1.8, "ridiculous": -2.0, "waste": -1.6,
    "delay": -1.0, "delayed": -1.2, "incorrect": -1.4, "overcharge": -1.8,
    "dispute": -1.5, "cancel": -1.0, "complaint": -1.8
}

FRUSTRATION_PATTERNS = [
    r"\b(speak to a manager|talk to a human|representative|human being|operator now)\b",
    r"\b(waste of time|wasting my time|ridiculous|unacceptable)\b",
    r"\b(how hard is it|cannot believe|already told you|repeat myself)\b",
    r"\b(lawyer|attorney|sue|court|legal action)\b",
    r"\b(why hasn'?t this been fixed|still not working|still broken)\b",
    r"\b(overcharged|charged twice|unauthorized charge)\b",
]

TOPIC_KEYWORDS = {
    "billing": ["invoice", "retainer", "payment", "card", "charge", "refund", "receipt", "balance", "fee", "cost"],
    "scheduling": ["schedule", "appointment", "calendar", "reschedule", "book", "date", "slot", "availability", "time"],
    "legal_intake": ["case", "filing", "court", "attorney", "lawyer", "contract", "retainer", "agreement", "claim"],
    "technical_support": ["broken", "error", "login", "password", "reset", "bug", "portal", "access", "connection"],
}


@dataclass
class TurnSentiment:
    turn_index: int
    speaker: str
    text: str
    polarity: str
    score: float
    is_frustrated: bool
    keywords: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CallSentimentAnalysis:
    overall_polarity: str
    overall_score: float  # -1.0 to 1.0
    caller_score: float
    agent_score: float
    frustration_detected: bool
    frustration_score: float  # 0.0 to 1.0
    frustration_reasons: List[str]
    sentiment_trajectory: List[str]  # e.g. ["negative", "neutral", "positive"]
    trajectory_trend: str  # "improving", "declining", "stable"
    turn_sentiments: List[Dict[str, Any]]
    positive_percentage: float
    neutral_percentage: float
    negative_percentage: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CallSummary:
    call_id: str
    executive_summary: str
    key_intent: str
    resolution_status: str
    action_items: List[str]
    topics: List[str]
    processed_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SentimentAnalyzer:
    """Zero-shot sentiment analysis and executive summary engine."""

    def __init__(self):
        pass

    def analyze_turn(self, text: str, speaker: str = "caller", turn_index: int = 1) -> TurnSentiment:
        """Analyzes sentiment for a single conversational speech turn."""
        clean_text = text.lower()
        words = re.findall(r"\b\w+\b", clean_text)

        matched_pos = []
        matched_neg = []
        raw_score = 0.0

        for w in words:
            if w in POSITIVE_LEXICON:
                val = POSITIVE_LEXICON[w]
                raw_score += val
                matched_pos.append(w)
            elif w in NEGATIVE_LEXICON:
                val = NEGATIVE_LEXICON[w]
                raw_score += val
                matched_neg.append(w)

        # Normalize score into range [-1.0, 1.0]
        token_count = max(1, len(words))
        norm_score = max(-1.0, min(1.0, raw_score / (math_factor := max(2.5, token_count * 0.4))))

        # Frustration detection
        is_frustrated = False
        if norm_score < -0.3:
            is_frustrated = True
        for pattern in FRUSTRATION_PATTERNS:
            if re.search(pattern, clean_text):
                is_frustrated = True
                norm_score = min(norm_score, -0.6)
                break

        if norm_score > 0.15:
            polarity = SentimentPolarity.POSITIVE.value
        elif norm_score < -0.15:
            polarity = SentimentPolarity.NEGATIVE.value
        else:
            polarity = SentimentPolarity.NEUTRAL.value

        keywords = matched_pos + matched_neg
        return TurnSentiment(
            turn_index=turn_index,
            speaker=speaker,
            text=text,
            polarity=polarity,
            score=round(norm_score, 3),
            is_frustrated=is_frustrated,
            keywords=keywords,
        )

    def analyze_call(
        self,
        transcript_turns: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CallSentimentAnalysis:
        """Analyzes sentiment across an entire call transcript."""
        if not transcript_turns:
            return CallSentimentAnalysis(
                overall_polarity=SentimentPolarity.NEUTRAL.value,
                overall_score=0.0,
                caller_score=0.0,
                agent_score=0.0,
                frustration_detected=False,
                frustration_score=0.0,
                frustration_reasons=[],
                sentiment_trajectory=["neutral"],
                trajectory_trend="stable",
                turn_sentiments=[],
                positive_percentage=0.0,
                neutral_percentage=100.0,
                negative_percentage=0.0,
            )

        turn_results: List[TurnSentiment] = []
        caller_scores = []
        agent_scores = []
        frustration_reasons = []

        for idx, turn in enumerate(transcript_turns):
            text = turn.get("text", turn.get("content", ""))
            speaker = turn.get("speaker", turn.get("role", "caller"))
            res = self.analyze_turn(text=text, speaker=speaker, turn_index=idx + 1)
            turn_results.append(res)

            if "caller" in speaker.lower() or "customer" in speaker.lower() or "user" in speaker.lower():
                caller_scores.append(res.score)
                if res.is_frustrated:
                    frustration_reasons.append(f"Turn {idx+1}: '{text[:60]}...'")
            else:
                agent_scores.append(res.score)

        avg_caller = sum(caller_scores) / max(1, len(caller_scores)) if caller_scores else 0.0
        avg_agent = sum(agent_scores) / max(1, len(agent_scores)) if agent_scores else 0.0

        # Overall score weights caller 70% and agent 30%
        overall_score = round(avg_caller * 0.7 + avg_agent * 0.3, 3)

        if overall_score > 0.12:
            overall_polarity = SentimentPolarity.POSITIVE.value
        elif overall_score < -0.12:
            overall_polarity = SentimentPolarity.NEGATIVE.value
        else:
            overall_polarity = SentimentPolarity.NEUTRAL.value

        # Frustration metrics
        frustration_detected = len(frustration_reasons) > 0
        frustration_score = min(1.0, round(len(frustration_reasons) / max(1, len(caller_scores)) * 2.0, 2))

        # Trajectory (start -> mid -> end)
        n = len(turn_results)
        if n >= 3:
            s1 = turn_results[0].polarity
            s2 = turn_results[n // 2].polarity
            s3 = turn_results[-1].polarity
            trajectory = [s1, s2, s3]
            start_num = turn_results[0].score
            end_num = turn_results[-1].score
            if end_num - start_num > 0.2:
                trend = "improving"
            elif start_num - end_num > 0.2:
                trend = "declining"
            else:
                trend = "stable"
        elif n == 2:
            trajectory = [turn_results[0].polarity, turn_results[1].polarity]
            diff = turn_results[1].score - turn_results[0].score
            trend = "improving" if diff > 0.15 else ("declining" if diff < -0.15 else "stable")
        else:
            trajectory = [turn_results[0].polarity]
            trend = "stable"

        pos_count = sum(1 for t in turn_results if t.polarity == SentimentPolarity.POSITIVE.value)
        neg_count = sum(1 for t in turn_results if t.polarity == SentimentPolarity.NEGATIVE.value)
        neu_count = n - (pos_count + neg_count)

        return CallSentimentAnalysis(
            overall_polarity=overall_polarity,
            overall_score=overall_score,
            caller_score=round(avg_caller, 3),
            agent_score=round(avg_agent, 3),
            frustration_detected=frustration_detected,
            frustration_score=frustration_score,
            frustration_reasons=frustration_reasons,
            sentiment_trajectory=trajectory,
            trajectory_trend=trend,
            turn_sentiments=[t.to_dict() for t in turn_results],
            positive_percentage=round((pos_count / n) * 100, 1),
            neutral_percentage=round((neu_count / n) * 100, 1),
            negative_percentage=round((neg_count / n) * 100, 1),
        )

    def generate_summary(
        self,
        call_id: str,
        transcript_turns: List[Dict[str, Any]],
        sentiment: Optional[CallSentimentAnalysis] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CallSummary:
        """Generates an executive narrative summary, identifies key intent, and extracts action items."""
        if not transcript_turns:
            return CallSummary(
                call_id=call_id,
                executive_summary="Empty call with no transcribed speech turns.",
                key_intent="Unknown",
                resolution_status=ResolutionStatus.INQUIRY.value,
                action_items=[],
                topics=[],
            )

        full_text = " ".join(t.get("text", "") for t in transcript_turns).lower()

        # Identify Topics
        detected_topics = []
        for topic, kws in TOPIC_KEYWORDS.items():
            if any(kw in full_text for kw in kws):
                detected_topics.append(topic)
        if not detected_topics:
            detected_topics = ["general_inquiry"]

        # Determine Primary Intent
        first_caller_turn = ""
        for t in transcript_turns:
            role = str(t.get("role", t.get("speaker", ""))).lower()
            if "caller" in role or "user" in role or "customer" in role:
                first_caller_turn = t.get("text", "")
                break

        if "billing" in detected_topics:
            key_intent = "Billing and invoice inquiry"
        elif "scheduling" in detected_topics:
            key_intent = "Appointment scheduling or rescheduling"
        elif "legal_intake" in detected_topics:
            key_intent = "Legal consultation and case intake"
        elif "technical_support" in detected_topics:
            key_intent = "Technical support and portal assistance"
        elif first_caller_turn:
            key_intent = first_caller_turn[:80].strip()
        else:
            key_intent = "Customer voice inquiry"

        # Determine Resolution Status
        has_transfer = any("transfer" in t.get("text", "").lower() for t in transcript_turns)
        has_resolution = any(w in full_text for w in ["confirmed", "cleared", "resolved", "rescheduled", "booked", "all set"])
        has_followup = any(w in full_text for w in ["email you", "follow up", "send you", "call you back", "looking into"])

        if has_transfer:
            resolution = ResolutionStatus.ESCALATED.value
        elif has_resolution:
            resolution = ResolutionStatus.RESOLVED.value
        elif has_followup:
            resolution = ResolutionStatus.PENDING_FOLLOWUP.value
        else:
            resolution = ResolutionStatus.INQUIRY.value

        # Extract Action Items
        action_items = []
        if "billing" in detected_topics:
            action_items.append("Send itemized billing statement to customer email")
        if "scheduling" in detected_topics:
            action_items.append("Send calendar appointment invite and SMS reminder")
        if "legal_intake" in detected_topics:
            action_items.append("Queue case intake documentation for attorney review")
        if has_transfer:
            action_items.append("Verify transfer handoff notes recorded in CRM")
        if not action_items:
            action_items.append("Log call completion record to customer profile")

        # Synthesize 2-3 sentence Executive Narrative Summary
        caller_name = (metadata or {}).get("caller_name", "Customer")
        topic_str = ", ".join(t.replace("_", " ") for t in detected_topics)
        status_str = resolution.replace("_", " ")

        summary_sentence_1 = f"{caller_name} connected regarding {topic_str}."
        if resolution == ResolutionStatus.RESOLVED.value:
            summary_sentence_2 = "The AI voice assistant successfully provided requested information and concluded the inquiry with full resolution."
        elif resolution == ResolutionStatus.ESCALATED.value:
            summary_sentence_2 = "The call required live agent intervention and was transferred to the appropriate department."
        else:
            summary_sentence_2 = "Follow-up actions were noted and will be dispatched to the customer."

        executive_summary = f"{summary_sentence_1} {summary_sentence_2}"

        return CallSummary(
            call_id=call_id,
            executive_summary=executive_summary,
            key_intent=key_intent,
            resolution_status=resolution,
            action_items=action_items,
            topics=detected_topics,
        )

    def analyze_and_summarize(
        self,
        call_id: str,
        transcript_turns: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Convenience method running both sentiment classification and executive summary generation."""
        sentiment = self.analyze_call(transcript_turns, metadata)
        summary = self.generate_summary(call_id, transcript_turns, sentiment, metadata)
        return {
            "call_id": call_id,
            "sentiment": sentiment.to_dict(),
            "summary": summary.to_dict(),
            "processed_at": time.time(),
        }


# Singleton instance
sentiment_analyzer = SentimentAnalyzer()
