"""The demo page (task 8.3): pick a role, ask, see the answer and everything behind it.

uv run streamlit run src/warehouse_qa/app.py      (or: make demo)
"""

from __future__ import annotations

from typing import Any

import streamlit as st
from warehouse_graph.cli import load_dotenv

from warehouse_qa.config import Env
from warehouse_qa.service import (
    ROLES,
    ROOT,
    Answer,
    Assistant,
    NotRecordedError,
    demo_questions,
)

ROLE_HELP = {
    "analyst_au": "Australian rows only; personal data masked; salaries hidden",
    "analyst_nz": "New Zealand rows only; personal data masked; salaries hidden",
    "marketing": "All regions; emails, phones and salaries hidden; names masked",
    "finance": "All regions; emails and phones masked; salaries visible",
    "admin": "Everything",
}


@st.cache_resource(show_spinner="Loading the graph and the local models (first run: ~1 min)…")
def assistant() -> Assistant:
    load_dotenv(Env.load(ROOT / "warehouse-qa.toml").graph.parents[1] / ".env")
    return Assistant()


def dot(answer: Answer) -> str:
    """The lineage as Graphviz (drawn in the browser)."""

    def name(node_id: str) -> str:
        return node_id.split(".", 1)[1]

    used = set(answer.tables)
    shapes = {"exposure": "note", "metric": "hexagon", "source": "cylinder"}
    nodes = {n for e in answer.lineage for n in e} | used
    lines = ["digraph { rankdir=LR; node [shape=box, fontname=Helvetica, fontsize=11];"]
    for n in sorted(nodes):
        shape = shapes.get(n.split(".", 1)[0], "box")
        style = ', style=filled, fillcolor="#cde2fb"' if n in used else ""
        lines.append(f'"{n}" [label="{name(n)}", shape={shape}{style}];')
    lines += [f'"{up}" -> "{down}";' for up, down in answer.lineage]
    return "\n".join([*lines, "}"])


def show(answer: Answer, live: bool) -> None:
    r = answer.record
    banner: Any = {"Answered": st.success, "Not answered": st.warning}.get(answer.outcome, st.info)
    if not answer.answered:
        head, *options = r.answer_text.split("\n")
        banner(f"**{answer.outcome}.** {head}")
        for option in options:
            st.markdown(option)
        return
    confidence = f" · confidence {r.confidence:.2f}" if r.confidence is not None else ""
    banner(f"**{answer.outcome}**{confidence}")
    for a in r.assumptions:
        st.markdown(f"**Read as:** {a}")
    for w in r.warnings:
        st.warning(w, icon="⚠️")
    if r.rows:
        st.dataframe([dict(zip(r.columns, row, strict=True)) for row in r.rows], hide_index=True)
    for line in r.answer_text.split("\n"):
        if line.startswith(("Groups of", "Your role", "Documented caveat")):
            st.caption(line)
    sql, lineage, checks = st.tabs(["SQL", "Lineage", "Checks"])
    sql.code(r.sql or "", language="sql", wrap_lines=True)
    lineage.caption("Tables this answer reads (shaded), as far as your role can see.")
    lineage.graphviz_chart(dot(answer))
    when = "" if live else " when it was recorded"
    checks.markdown(
        f"- Verification: {'; '.join(r.checks)}\n- Repairs: {r.repairs}\n"
        f"- Context given to the model: {r.context_tokens:,} tokens"
        f" from {len(r.retrieved_nodes)} retrieved nodes\n"
        f"- Model cost{when}: US${r.usage.usd:.4f}"
    )


def main() -> None:
    st.set_page_config(page_title="warehouse-qa", page_icon="🔎", layout="wide")
    st.title("Ask the Kiwi Retail warehouse")
    st.caption(
        "Every query runs as the selected role's own database login. The database decides what"
        " comes back; the model only writes SQL."
    )
    bot = assistant()
    if not bot.live:
        st.info("No API key: replaying recorded model replies for the example questions below.")

    with st.sidebar:
        st.header("Who is asking?")
        examples = demo_questions()
        example = st.selectbox(
            "Example",
            [None, *examples],
            format_func=lambda e: "Choose an example…" if e is None else f"{e.role}: {e.question}",
        )
        role = st.radio(
            "Role",
            ROLES,
            index=ROLES.index(example.role) if example else 0,
            captions=[ROLE_HELP[r] for r in ROLES],
        )
        if example:
            st.caption(f"Shows: {example.shows}")

    question = st.text_input("Question", value=example.question if example else "")
    if st.button("Ask", type="primary", disabled=not question.strip()):
        with st.spinner("Retrieving, writing SQL, checking it, running it as the role…"):
            try:
                show(bot.ask(role, question.strip()), bot.live)
            except NotRecordedError as e:
                st.error(str(e))


main()
