"""Core statement types. Money is integer paise everywhere inside the pipeline."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Txn(BaseModel):
    date: str | None  # ISO yyyy-mm-dd
    value_date: str | None = None
    description: str = ""
    ref: str | None = None
    debit: int = 0  # paise
    credit: int = 0  # paise
    balance: int | None = None  # paise; negative = overdrawn
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    raw: dict[str, str] = Field(default_factory=dict)  # role -> cell text as printed
    ambiguous: list[str] = Field(default_factory=list)  # reasons the parser was unsure
    notes: list[str] = Field(default_factory=list)  # e.g. "zero_amount" (failed txn, no effect)
    source: str = "parser"  # parser | llm


class StatementMeta(BaseModel):
    bank_name: str | None = None
    account_holder: str | None = None
    account_masked: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    opening_balance: int | None = None  # paise
    closing_balance: int | None = None  # paise
    currency: str | None = "INR"


# ---------------- LLM I/O schemas (rupees as numbers; converted to paise in code) -------------
class LLMMeta(BaseModel):
    bank_name: str | None
    account_holder: str | None
    account_number: str | None = Field(description="as printed")
    period_start: str | None = Field(description="YYYY-MM-DD")
    period_end: str | None = Field(description="YYYY-MM-DD")
    opening_balance: float | None
    closing_balance: float | None
    currency: str | None


class LLMRow(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    description: str
    debit: float | None
    credit: float | None
    balance: float | None


class LLMRows(BaseModel):
    rows: list[LLMRow]
