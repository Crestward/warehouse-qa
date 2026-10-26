"""Eval records (task 4.1): the labelled questions and red-team prompts, and their loaders.

The labelling rules are in docs/eval.md. In short:
- gold SQL is executed **as the question's role**, so gold results already reflect RLS and masking
- a question whose correct answer for that role is "not permitted" is `refuse`, with no gold SQL
- `clarify` and `abstain` questions have no gold SQL either
- `gold_nodes` are the graph node ids a good retriever should surface: the tables and columns
  the gold SQL reads (derived, see labels.py) plus the metric and caveats a careful analyst
  would consult
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Mode = Literal["answer", "answer_with_assumption", "clarify", "abstain", "refuse"]
Category = Literal["regular", "trap", "clarify_abstain"]
Difficulty = Literal["easy", "medium", "hard"]
Role = Literal["admin", "finance", "marketing", "analyst_au", "analyst_nz"]
TrapType = Literal[
    "deprecated_model",  # T1
    "deprecated_column",  # T2
    "stale_source",  # T3
    "competing_metric",  # T4, T5
    "fanout_join",  # T6, T7
    "misleading_name",  # T8, T9
    "data_gap",  # T10
    "currency",  # T11
    "ambiguous_dimension",  # T12
    "moving_now",  # T13
]
RedTeamCategory = Literal["direct", "small_cell", "injection", "role_confusion", "sql_escalation"]

EVAL_DIR = Path("evals")
QUESTIONS = EVAL_DIR / "questions.yaml"
REDTEAM = EVAL_DIR / "redteam.yaml"
QUESTION_COUNT = 150  # PRD §1.4.1: exactly; 40 trap + 20 clarify/abstain + 90 regular
REDTEAM_COUNT = 50


class EvalQuestion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^q\d{3}$")
    category: Category
    difficulty: Difficulty
    role: Role
    question: str
    expected_mode: Mode
    gold_sql: str | None = None
    gold_nodes: tuple[str, ...] = ()
    trap_type: TrapType | None = None
    trap_nodes: tuple[str, ...] = ()  # nodes the final SQL must not read (the trap's bait)
    assumption: str | None = None  # answer_with_assumption: the default a good answer states
    clarify_options: tuple[str, ...] = ()  # clarify: the readings a good question offers
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> EvalQuestion:
        has_sql = self.expected_mode in ("answer", "answer_with_assumption")
        if has_sql != (self.gold_sql is not None):
            raise ValueError(f"{self.id}: gold_sql is required exactly for answer modes")
        if self.trap_nodes and self.category != "trap":
            raise ValueError(f"{self.id}: trap_nodes only belong on trap questions")
        if (self.category == "trap") != (self.trap_type is not None):
            raise ValueError(f"{self.id}: trap_type is required exactly for trap questions")
        if self.expected_mode == "answer_with_assumption" and not self.assumption:
            raise ValueError(f"{self.id}: answer_with_assumption needs the assumption")
        if self.expected_mode == "clarify" and len(self.clarify_options) < 2:
            raise ValueError(f"{self.id}: clarify needs at least two options")
        if self.category == "clarify_abstain" and self.expected_mode not in ("clarify", "abstain"):
            raise ValueError(f"{self.id}: clarify_abstain questions must clarify or abstain")
        return self


class RedTeamPrompt(BaseModel):
    """An attack. Success = any leakage (docs/eval.md, PRD §3.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^r\d{3}$")
    category: RedTeamCategory
    role: Role
    prompt: str
    target: str  # what an attacker wants, e.g. "dim_customers.email values"
    expected_mode: Literal["refuse", "answer"]  # answer = a safe, benign answer exists
    notes: str | None = None


def write_text_lf(path: Path, text: str) -> None:
    """Write UTF-8 with LF line endings on every OS (committed files are hashed)."""
    path.write_bytes(text.encode("utf-8"))


def file_sha256(path: Path) -> str:
    """sha256 with line endings normalised, so a Windows checkout hashes like CI does."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_questions(path: Path = QUESTIONS) -> list[EvalQuestion]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    questions = [EvalQuestion.model_validate(q) for q in data["questions"]]
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate question ids")
    return questions


def load_redteam(path: Path = REDTEAM) -> list[RedTeamPrompt]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    prompts = [RedTeamPrompt.model_validate(p) for p in data["prompts"]]
    ids = [p.id for p in prompts]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate red-team ids")
    return prompts
