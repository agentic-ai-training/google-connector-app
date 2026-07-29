"""Structured content requirements shared by planning, execution, and verification.

This module deliberately describes *classes* of content work.  It does not map
individual user sentences to canned answers.  The resulting contract is safe to
persist in a plan and lets later stages verify that the artifact being delivered
is the artifact the current request asked for.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


LANGUAGE_ALIASES = {
    "arabic": "Arabic",
    "cantonese": "Cantonese",
    "egyptian": "Egyptian Arabic",
    "egyptian arabic": "Egyptian Arabic",
    "english": "English",
    "french": "French",
    "hebrew": "Hebrew",
    "japanese": "Japanese",
    "korean": "Korean",
    "pashto": "Pashto",
    "russian": "Russian",
    "slovak": "Slovak",
    "slovakian": "Slovak",
    "somali": "Somali",
    "somalian": "Somali",
    "spanish": "Spanish",
}

CONTENT_KINDS = (
    ("conversation", r"\b(?:conversation|dialogue|dialog|chat transcript)\b"),
    ("email", r"\b(?:email|mail|cover letter)\b"),
    ("application", r"\bapplication\b"),
    ("essay", r"\bessay\b"),
    ("roadmap", r"\broadmap\b"),
    ("list", r"\b(?:bullet points?|pointers?|checklist|outline)\b"),
    ("paragraph", r"\bparagraph\b"),
    ("message", r"\b(?:message|caption|post)\b"),
    ("document", r"\b(?:document|memo|report|content|prose|text)\b"),
)

CREATE_CONTENT_PATTERN = re.compile(
    r"\b(?:write|draft|compose|translate|"
    r"rewrite|revise|polish|summarize|summarise|outline|brainstorm|explain)\b",
    re.IGNORECASE,
)
CREATE_NAMED_CONTENT_PATTERN = re.compile(
    r"\b(?:create|make|generate|prepare|produce)\b.{0,80}\b"
    r"(?:paragraph|prose|letter|memo|caption|script|message|draft|content|"
    r"essay|roadmap|application|conversation|dialogue|text|report)\b",
    re.IGNORECASE,
)
INTERACTIVE_CONTENT_PATTERN = re.compile(
    r"\b(?:have|start|continue|hold)\s+(?:a\s+)?conversation\b|"
    r"\b(?:speak|talk|reply|respond)\s+(?:to\s+me\s+)?(?:in|using)\b",
    re.IGNORECASE,
)
WORD_TRANSLATION_PATTERN = re.compile(
    r"\b(?:each|every|all)\s+(?:individual\s+)?words?"
    r"(?:['’]?\s+translations?|\b.{0,30}\btranslat(?:e|ed|ion|ions))\b|"
    r"\btranslat(?:e|ed|ion|ions)\b.{0,30}"
    r"\b(?:each|every|all)\s+(?:individual\s+)?words?\b|"
    r"\bword[- ]by[- ]word\b",
    re.IGNORECASE,
)
WAIT_PATTERN = re.compile(
    r"\b(?:wait|hold)\b.{0,120}\b(?:next|later|another)\s+"
    r"(?:instruction|command|message)\b",
    re.IGNORECASE,
)
PROSPECTIVE_QUESTION = (
    "What conversation text should be sent? Paste it, or say "
    "'generate a sample now'."
)
MULTILINGUAL_FORMAT_QUESTION = (
    "Should each language have its own complete passage, or should the "
    "languages be mixed within one passage?"
)
GLOSS_LANGUAGE_QUESTION = (
    "Which language should be used for the word-by-word gloss?"
)


@dataclass(frozen=True)
class ContentContract:
    requested: bool = False
    kind: str = "none"
    mode: str = "none"
    languages: list[str] = field(default_factory=list)
    translation_granularity: str = "none"
    min_words: int = 0
    visible_output_budget: int = 0
    complexity: str = "low"
    prospective_artifact: bool = False
    deferred_delivery: bool = False
    required_clarifications: list[str] = field(default_factory=list)

    def plan_value(self) -> dict:
        return {"version": "content-contract-v1", **asdict(self)}


def _kind(text: str) -> str:
    for name, pattern in CONTENT_KINDS:
        if re.search(pattern, text, re.IGNORECASE):
            return name
    return "response"


def _languages(text: str) -> list[str]:
    normalized = text.casefold()
    matches = []
    for alias, canonical in sorted(
        LANGUAGE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True,
    ):
        match = re.search(rf"\b{re.escape(alias)}\b", normalized)
        if match:
            matches.append((match.start(), -len(alias), canonical))
    found = []
    for _, _, canonical in sorted(matches):
        if canonical not in found:
            found.append(canonical)
    return found


def analyze_content_request(message: str) -> ContentContract:
    """Create a provider-independent contract for bounded content generation."""
    normalized = " ".join(str(message or "").split())
    creation = bool(
        CREATE_CONTENT_PATTERN.search(normalized)
        or CREATE_NAMED_CONTENT_PATTERN.search(normalized)
        or INTERACTIVE_CONTENT_PATTERN.search(normalized)
    )
    if not creation:
        return ContentContract()

    kind = _kind(normalized)
    languages = _languages(normalized)
    word_translation = bool(WORD_TRANSLATION_PATTERN.search(normalized))
    prospective = bool(
        INTERACTIVE_CONTENT_PATTERN.search(normalized)
        and re.search(
            r"\b(?:then|after(?:wards)?|and)\b.{0,100}"
            r"\b(?:copy|send|share|email|post)\b",
            normalized,
            re.IGNORECASE,
        )
    )
    lowered = normalized.casefold()
    prospective_answered = f"{PROSPECTIVE_QUESTION.casefold()}:" in lowered
    multilingual_format_answered = (
        f"{MULTILINGUAL_FORMAT_QUESTION.casefold()}:" in lowered
    )
    gloss_language_answered = f"{GLOSS_LANGUAGE_QUESTION.casefold()}:" in lowered
    if prospective_answered:
        prospective = False
    min_words = {
        "paragraph": 12,
        "essay": 80,
        "conversation": 12,
        "list": 6,
    }.get(kind, 4)
    complexity_score = (
        len(languages)
        + (4 if word_translation else 0)
        + (2 if kind in {"essay", "roadmap", "application"} else 0)
    )
    complexity = (
        "high" if complexity_score >= 6 else
        "medium" if complexity_score >= 2 else "low"
    )
    # This is a visible-answer allowance, not an instruction to consume it.
    # Reasoning models may account for hidden reasoning separately.
    visible_budget = {
        "low": 1_200,
        "medium": 2_400,
        "high": 4_000,
    }[complexity]
    clarifications = []
    if prospective:
        clarifications.append(PROSPECTIVE_QUESTION)
    if word_translation and len(languages) > 4:
        if not multilingual_format_answered:
            clarifications.append(MULTILINGUAL_FORMAT_QUESTION)
        if not gloss_language_answered:
            clarifications.append(GLOSS_LANGUAGE_QUESTION)
    return ContentContract(
        requested=True,
        kind=kind,
        mode="interactive" if prospective else "generate",
        languages=languages,
        translation_granularity="word" if word_translation else (
            "passage" if len(languages) > 1 else "none"
        ),
        min_words=min_words,
        visible_output_budget=visible_budget,
        complexity=complexity,
        prospective_artifact=prospective,
        deferred_delivery=bool(WAIT_PATTERN.search(normalized)),
        required_clarifications=clarifications,
    )
