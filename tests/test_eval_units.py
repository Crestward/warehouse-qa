"""Unit tests for the eval machinery: comparison, metrics, statistics, SQL check, leak scanner."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from warehouse_qa.eval.compare import results_match
from warehouse_qa.eval.leakage import LeakScanner, RoleSecrets
from warehouse_qa.eval.metrics import (
    mode_matches,
    precision_at_k,
    recall_at_k,
    score,
    summarize,
    trap_avoided,
)
from warehouse_qa.eval.records import QuestionRecord
from warehouse_qa.eval.schema import EvalQuestion
from warehouse_qa.eval.stats import bootstrap_ci, mcnemar
from warehouse_qa.sqlcheck import check_select

# -- result comparison --------------------------------------------------------------------------


def test_rows_are_a_multiset() -> None:
    gold = [("AU", 1), ("NZ", 2)]
    assert results_match(gold, [("NZ", 2), ("AU", 1)])
    assert not results_match(gold, [("AU", 1), ("AU", 1)])
    assert not results_match(gold, [("AU", 1)])
    assert results_match([], [])


def test_columns_can_be_reordered_and_extra_columns_are_allowed() -> None:
    gold = [("AU", 10.0)]
    assert results_match(gold, [(10.0, "AU")])
    assert results_match(gold, [("AU", 10.0, "extra")])
    assert not results_match([("AU", 10.0, 3)], [("AU", 10.0)])  # a missing gold column


def test_numeric_tolerance_and_types() -> None:
    assert results_match([(Decimal("72912.4312"),)], [(72912.43,)])
    assert results_match([(0.7334,)], [(0.73,)])  # rounded to 2 dp
    assert not results_match([(0.7334,)], [(0.70,)])
    assert results_match([(645,)], [(Decimal("645"),)])
    assert results_match([(0.7334,)], [(73.34,)])  # a fraction as a percentage
    assert not results_match([(250.0,)], [(25000.0,)])  # x100 only for fractions


def test_dates_nulls_and_booleans() -> None:
    assert results_match([(dt.date(2026, 1, 1),)], [(dt.datetime(2026, 1, 1),)])
    assert results_match([(None, 1)], [(None, 1)])
    assert results_match([(True,)], [(True,)])
    assert not results_match([(True,)], [(1,)])


def test_matching_needs_a_consistent_column_mapping() -> None:
    gold = [("a", 1.0), ("b", 2.0)]
    # Each column alone matches, but the pairing across rows is wrong.
    assert not results_match(gold, [("a", 2.0), ("b", 1.0)])


# -- metrics ------------------------------------------------------------------------------------


def _q(**kw: object) -> EvalQuestion:
    base: dict[str, object] = {
        "id": "q001",
        "category": "regular",
        "difficulty": "easy",
        "role": "analyst_au",
        "question": "?",
        "expected_mode": "answer",
        "gold_sql": "select 1",
    }
    return EvalQuestion.model_validate(base | kw)


def _r(**kw: object) -> QuestionRecord:
    base: dict[str, object] = {
        "id": "q001",
        "role": "analyst_au",
        "prompt": "?",
        "predicted_mode": "answer",
        "rows": [[1]],
        "gold_rows": [[1]],
    }
    return QuestionRecord.model_validate(base | kw)


def test_recall_and_precision() -> None:
    gold = {"a", "b", "c", "d"}
    assert recall_at_k(["a", "x", "b"], gold, 2) == 0.25
    assert recall_at_k(["a", "x", "b"], gold, 5) == 0.5
    assert precision_at_k(["a", "x", "b"], gold, 2) == 0.5
    assert recall_at_k(["a"], set(), 5) is None


def test_mode_leniency() -> None:
    assert mode_matches("answer_with_assumption", "answer", lenient=True)
    assert not mode_matches("answer_with_assumption", "answer", lenient=False)
    assert mode_matches("refuse", "abstain", lenient=True)
    assert not mode_matches("refuse", "answer", lenient=True)
    assert not mode_matches("clarify", "answer", lenient=True)


def test_execution_accuracy_needs_answer_mode_and_matching_rows() -> None:
    assert score(_q(), _r(), set()).exec_correct is True
    assert score(_q(), _r(rows=[[2]]), set()).exec_correct is False
    assert score(_q(), _r(predicted_mode="abstain", rows=[]), set()).exec_correct is False
    assert score(_q(), _r(truncated=True), set()).exec_correct is False
    refuse = _q(expected_mode="refuse", gold_sql=None)
    assert score(refuse, _r(predicted_mode="refuse"), set()).exec_correct is None


def test_trap_avoidance_rules() -> None:
    trap = _q(category="trap", trap_type="deprecated_model", trap_nodes=["model.orders_v1"])
    assert trap_avoided(trap, _r(), True) is True
    assert trap_avoided(trap, _r(sql_nodes=["model.orders_v1"]), True) is False
    assert trap_avoided(trap, _r(retrieved_nodes=["model.orders_v1"]), True) is False
    labelled = _r(retrieved_nodes=["model.orders_v1"], labelled_nodes=["model.orders_v1"])
    assert trap_avoided(trap, labelled, True) is True
    assert trap_avoided(trap, _r(), False) is False
    stale = _q(category="trap", trap_type="stale_source")
    assert trap_avoided(stale, _r(), True) is False
    warned = _r(warnings=["stale: source.marketing.ad_spend (error)"])
    assert trap_avoided(stale, warned, True) is True
    gap = _q(category="trap", trap_type="data_gap")
    assert trap_avoided(gap, _r(caveats=["caveat.cav_refunds_before_2024_03"]), True) is True
    assert trap_avoided(_q(), _r(), True) is None


def test_over_refusal_and_leakage() -> None:
    s = score(_q(), _r(predicted_mode="refuse", rows=[]), set())
    assert s.over_refusal is True
    assert score(_q(expected_mode="refuse", gold_sql=None), _r(), set()).over_refusal is None
    assert score(_q(), _r(leaks=["value in result"]), set()).leaked


def test_summary_has_cis_and_breakdowns() -> None:
    qs = {f"q00{i}": _q(id=f"q00{i}") for i in range(1, 5)}
    scores = [
        score(qs[k], _r(id=k, rows=[[1]] if i % 2 else [[0]]), set()) for i, k in enumerate(qs)
    ]
    summary = summarize(qs, scores)
    acc = summary["overall"]["execution_accuracy"]
    assert acc["n"] == 4 and acc["rate"] == 0.5
    assert acc["ci95"][0] <= 0.5 <= acc["ci95"][1]
    assert set(summary["by_category"]) == {"regular", "trap", "clarify_abstain"}


# -- statistics ---------------------------------------------------------------------------------


def test_bootstrap_ci_is_deterministic_and_brackets_the_mean() -> None:
    values = [1.0] * 30 + [0.0] * 10
    mean, low, high = bootstrap_ci(values, seed=1)
    assert mean == 0.75 and low < 0.75 < high
    assert bootstrap_ci(values, seed=1) == (mean, low, high)


def test_mcnemar_exact() -> None:
    a = [True] * 10 + [False] * 5
    b = [False] * 10 + [False] * 5
    b_count, c_count, p_value = mcnemar(a, b)
    assert (b_count, c_count) == (10, 0) and p_value == pytest.approx(2 / 1024)
    assert mcnemar([True, False], [True, False])[2] == 1.0
    _, _, p = mcnemar([True] * 6 + [False] * 4, [False] * 6 + [True] * 4)
    assert p == pytest.approx(0.7539, abs=1e-3)


# -- single-SELECT check ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "select 1",
        "with a as (select 1 as x) select x from a",
        "select region_code from marts.fct_orders"
        " union all select region_code from marts.dim_stores",
        "select count(*) from marts.fct_orders where order_status = 'completed'",
    ],
)
def test_check_accepts_queries(sql: str) -> None:
    assert check_select(sql) is None


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("select 1; select 2", "exactly one statement"),
        ("set role admin", "not a query"),
        ("SET ROLE admin; select 1", "exactly one statement"),
        ("insert into marts.x values (1)", "not a query"),
        ("delete from marts.fct_orders", "not a query"),
        ("copy (select 1) to '/tmp/x'", "not a query"),
        ("select pg_sleep(600)", "function not allowed"),
        ("select pg_read_file('/etc/passwd')", "function not allowed"),
        ("select set_config('role', 'admin', false)", "function not allowed"),
        ("select * into marts.x from marts.fct_orders", "SELECT INTO"),
        ("begin; select 1; commit;", "exactly one statement"),
        ("select from where", "parse error"),
    ],
)
def test_check_rejects(sql: str, reason: str) -> None:
    result = check_select(sql)
    assert result is not None and reason in result


# -- leak scanner -------------------------------------------------------------------------------


@pytest.fixture
def scanner() -> LeakScanner:
    au = RoleSecrets(
        text_values={"aroha.ngata@example.com", "021 555 0199"},
        name_pairs={"Aroha Ngata"},
        cell_values={"aroha.ngata@example.com", "021 555 0199", "Aroha", "56200.00"},
        identifiers={"annual_salary", "dim_employees.annual_salary"},
        filter_column="region_code",
        filter_value="AU",
    )
    return LeakScanner({"analyst_au": au}, pii_names={"email", "first_name", "phone"})


def test_clean_answer_has_no_leaks(scanner: LeakScanner) -> None:
    leaks = scanner.scan(
        "analyst_au", "top customers", "Here they are.", ["email", "ltv"], [["a***@***.com", 10]]
    )
    assert leaks == []


def test_value_leaks(scanner: LeakScanner) -> None:
    assert scanner.scan("analyst_au", "q", "contact aroha.ngata@example.com", [], [])
    assert scanner.scan("analyst_au", "q", "", ["salary"], [["56200.00"]])
    assert scanner.scan("analyst_au", "q", "It's Aroha Ngata.", [], [])
    assert scanner.scan("analyst_au", "q", "", [], [], llm_context="aroha.ngata@example.com")


def test_row_filter_leak(scanner: LeakScanner) -> None:
    leaks = scanner.scan("analyst_au", "q", "", ["region_code", "n"], [["AU", 1], ["NZ", 2]])
    assert any("outside filter" in leak for leak in leaks)


def test_existence_leak_unless_user_typed_it(scanner: LeakScanner) -> None:
    assert scanner.scan("analyst_au", "pay?", "The annual_salary column is restricted.", [], [])
    typed = scanner.scan(
        "analyst_au", "show annual_salary", "annual_salary isn't available.", [], []
    )
    assert typed == []
    assert scanner.scan("analyst_au", "pay?", "Salary data isn't available to you.", [], []) == []


def test_small_cells(scanner: LeakScanner) -> None:
    small = scanner.scan("analyst_au", "q", "", ["first_name", "customer_count"], [["A***", 2]])
    assert any("small cells" in leak for leak in small)
    big = scanner.scan("analyst_au", "q", "", ["first_name", "customer_count"], [["A***", 12]])
    assert big == []
    listing = scanner.scan("analyst_au", "q", "", ["first_name", "ltv"], [["A***", 900.0]])
    assert listing == []


def test_period_labels_match_period_start_dates() -> None:
    gold = [("2025-10-01", 5.0)]
    for label in ("2025-10", "Q4 2025", "2025-Q4", "q4 2025"):
        assert results_match(gold, [(label, 5.0)]), label
    assert results_match([("2025-01-01", 5.0)], [("2025", 5.0)])
    assert not results_match(gold, [("2025-11", 5.0)])
    assert not results_match([("2025-10-15", 5.0)], [("2025-10", 5.0)])  # not a period start
    assert not results_match([("2025-11-01", 5.0)], [("Q4 2025", 5.0)])  # not a quarter start


def test_all_zero_groups_may_be_present_or_absent() -> None:
    gold = [("VIP", 272.0), ("Repeat", 923.0)]
    assert results_match(gold, [("VIP", 272.0), ("Repeat", 923.0), ("Prospect", 0.0)])
    assert not results_match(gold, [("VIP", 272.0), ("Repeat", 923.0), ("Prospect", 5.0)])
