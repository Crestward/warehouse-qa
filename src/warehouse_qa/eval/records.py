"""One JSONL line per question per run: everything needed to re-score offline (task 4.8).

A record stores what the system did (mode, SQL, result, retrieved nodes, warnings, cost) and the
gold result for that question executed as its role, so `eval rescore` needs neither the database
nor an LLM. Leak findings are stored too: finding them needs the database's forbidden values.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from warehouse_qa.eval.schema import Mode

ROW_CAP = 1000  # rows kept per result; larger results are truncated (and flagged)


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usd: float = 0.0
    cache_hits: int = 0


class QuestionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str = "question"  # question | redteam
    role: str
    prompt: str
    predicted_mode: Mode
    answer_text: str = ""
    assumptions: list[str] = Field(default_factory=list)
    sql: str | None = None
    sql_check: str | None = None  # None = passed, else why it was rejected
    error: str | None = None  # execution error class + first line
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    truncated: bool = False
    gold_rows: list[list[Any]] | None = None
    gold_error: str | None = None
    retrieved_nodes: list[str] = Field(default_factory=list)  # ranked
    labelled_nodes: list[str] = Field(default_factory=list)  # retrieved with a warning label
    sql_nodes: list[str] = Field(default_factory=list)  # nodes the system's SQL read
    warnings: list[str] = Field(default_factory=list)  # e.g. "stale: source.marketing.ad_spend"
    caveats: list[str] = Field(default_factory=list)  # caveat ids surfaced in the answer
    context_tokens: int = 0
    usage: Usage = Field(default_factory=Usage)
    latency_s: float = 0.0
    leaks: list[str] = Field(default_factory=list)
