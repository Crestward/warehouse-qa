"""Static SQL check, minimal version (task 4.10): exactly one read-only SELECT.

Phase 6 (task 6.2) extends this with allow-listed schemas/functions and LIMIT injection. The
database would refuse most of these anyway (read-only login roles, D13); rejecting them here
gives a clean outcome and keeps them out of the database log.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

FORBIDDEN_FUNCTIONS = {
    "pg_sleep",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "dblink",
    "dblink_exec",
    "set_config",
    "pg_terminate_backend",
    "pg_cancel_backend",
}
FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,  # SET, COPY, GRANT, BEGIN, ... that sqlglot doesn't model
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Copy,
)


def check_select(sql: str) -> str | None:
    """None if `sql` is exactly one read-only query, else the reason it is rejected."""
    try:
        statements = [s for s in sqlglot.parse(sql, dialect="postgres") if s is not None]
    except SqlglotError as e:
        return f"parse error: {str(e).splitlines()[0][:200]}"
    if len(statements) != 1:
        return f"expected exactly one statement, got {len(statements)}"
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        return f"not a query: {type(tree).__name__}"
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            return f"forbidden statement: {type(node).__name__}"
        if isinstance(node, exp.Func):
            name = (node.sql_name() if not isinstance(node, exp.Anonymous) else node.name).lower()
            if name in FORBIDDEN_FUNCTIONS:
                return f"forbidden function: {name}"
        if isinstance(node, exp.Into):
            return "SELECT INTO is not allowed"
    return None
