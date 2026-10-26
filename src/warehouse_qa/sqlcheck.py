"""Static SQL checks (task 6.2, PRD §3.3 step 5). Pure: nothing touches the database.

    1. parses (sqlglot, postgres)
    2. exactly one statement, and it is a query: no DDL/DML, SET/RESET ROLE, transaction
       control, COPY, SELECT INTO
    3. tables only from allow-listed schemas (`marts`); CTE names are fine
    4. functions only from an allow-list (aggregates, window, date, string, math); anything
       else, e.g. pg_sleep, pg_read_file, dblink, set_config, is rejected
    5. a LIMIT is injected when the outer query has none

The database would refuse most of this anyway (read-only login roles, D13). Rejecting it here
gives a clean outcome, keeps it out of the database log, and is defence in depth.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

ALLOWED_SCHEMAS = frozenset({"marts"})

# sqlglot expression classes that are safe, read-only functions.
ALLOWED_FUNCTIONS = frozenset(
    {
        # logic and conditionals
        "And", "Or", "Not", "Xor", "Case", "If", "Coalesce", "Nullif", "Greatest", "Least",
        "Exists", "In", "Between", "Cast", "TryCast",
        # aggregates
        "Count", "CountIf", "Sum", "Avg", "Min", "Max", "Median", "Stddev", "StddevPop",
        "StddevSamp", "Variance", "VariancePop", "PercentileCont", "PercentileDisc",
        "LogicalOr", "LogicalAnd", "GroupConcat", "ArrayAgg", "AnyValue", "ApproxDistinct",
        # window
        "RowNumber", "Rank", "DenseRank", "PercentRank", "CumeDist", "Ntile", "Lag", "Lead",
        "FirstValue", "LastValue", "NthValue",
        # dates
        "Extract", "DateTrunc", "TimestampTrunc", "TimeToStr", "StrToTime", "StrToDate",
        "DateAdd", "DateSub", "DateDiff", "TsOrDsAdd", "CurrentDate", "CurrentTimestamp",
        "Date", "DateFromParts", "MakeInterval", "TimeStrToDate",
        # strings
        "Trim", "Upper", "Lower", "Length", "Substring", "Concat", "ConcatWs", "Left", "Right",
        "Lpad", "Rpad", "Replace", "Initcap", "StrPosition", "SplitPart", "RegexpLike",
        # math
        "Round", "Abs", "Floor", "Ceil", "Sqrt", "Pow", "Ln", "Log", "Exp", "Mod", "Sign",
        "SafeDivide", "Div",
    }
)  # fmt: skip
# Functions sqlglot doesn't model (exp.Anonymous), by name.
ALLOWED_ANONYMOUS = frozenset(
    {"make_date", "date_part", "age", "to_char", "to_date", "string_agg", "bool_or", "bool_and"}
)


@dataclass(frozen=True)
class CheckedSql:
    sql: str  # with the LIMIT injected
    limit_injected: bool


class StaticCheckError(ValueError):
    """The query was rejected before reaching the database. `str(e)` is the reason."""


def _statements(sql: str) -> list[exp.Expr]:
    try:
        return [s for s in sqlglot.parse(sql, dialect="postgres") if s is not None]
    except SqlglotError as e:
        raise StaticCheckError(f"parse error: {str(e).splitlines()[0][:200]}") from None


def check_select(sql: str) -> str | None:
    """None if `sql` passes every check, else the first reason it is rejected."""
    try:
        check_sql(sql, row_limit=None)
    except StaticCheckError as e:
        return str(e)
    return None


def check_sql(sql: str, row_limit: int | None = 1000) -> CheckedSql:
    statements = _statements(sql)
    if len(statements) != 1:
        raise StaticCheckError(f"expected exactly one statement, got {len(statements)}")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise StaticCheckError(f"not a query: {type(tree).__name__}")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for node in tree.walk():
        if isinstance(node, exp.Into):
            raise StaticCheckError("SELECT INTO is not allowed")
        if isinstance(
            node,
            exp.Insert | exp.Update | exp.Delete | exp.Merge | exp.Create | exp.Drop | exp.Alter
            | exp.Command | exp.Set | exp.Transaction | exp.Commit | exp.Rollback | exp.Copy,
        ):  # fmt: skip
            raise StaticCheckError(f"forbidden statement: {type(node).__name__}")
        if isinstance(node, exp.Table) and node.name:
            schema = node.db.lower()
            if not schema and node.name.lower() in ctes:
                continue
            if schema not in ALLOWED_SCHEMAS:
                where = f"{schema}.{node.name}" if schema else node.name
                raise StaticCheckError(f"table outside the allowed schemas: {where}")
        if isinstance(node, exp.Anonymous):
            if node.name.lower() not in ALLOWED_ANONYMOUS:
                raise StaticCheckError(f"function not allowed: {node.name.lower()}")
        elif isinstance(node, exp.Func) and type(node).__name__ not in ALLOWED_FUNCTIONS:
            raise StaticCheckError(f"function not allowed: {node.sql_name().lower()}")

    if row_limit is None or tree.args.get("limit") is not None:
        return CheckedSql(sql=sql.strip().rstrip(";"), limit_injected=False)
    if isinstance(tree, exp.Select):
        limited: exp.Expression = tree.limit(row_limit)
    else:  # UNION etc.: wrap it
        limited = exp.select("*").from_(tree.subquery("q")).limit(row_limit)
    return CheckedSql(sql=limited.sql(dialect="postgres"), limit_injected=True)
