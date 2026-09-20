"""Historique, budget de contexte, citations et prompt système."""

from __future__ import annotations

import pytest

from archipelle.agent import citations, prompts
from archipelle.agent.budget import (
    ContextSaturated,
    estimate_tokens,
    fit,
    item_tokens,
    marker_for,
    tree_budget_chars,
)
from archipelle.agent.history import History, IgnoredRecord, Source, interruption_item
from archipelle.providers.catalog import ModelInfo, load_catalog
from archipelle.providers.pivot import (
    AssistantMessage,
    PivotItem,
    SystemNotice,
    ToolCall,
    ToolResult,
    ToolResultGroup,
    TreeMessage,
    UserMessage,
)

CALL = ToolCall.from_arguments("c1", "read_file", {"path": "bail.pdf"})


def _turn(history: History, conversation_id: str, run_id: str = "r1"):
    return history.open_turn(conversation_id, run_id, "quick", "/travail")


# --- historique ----------------------------------------------------------------------


def test_conversation_lifecycle(history: History) -> None:
    conversation = history.create_conversation("mistral", "m", workdir="/travail")
    assert history.get_conversation(conversation.id) == conversation
    history.update_conversation(conversation.id, title="Bail", provider_locked=True, model="m2")
    reloaded = history.get_conversation(conversation.id)
    assert reloaded is not None
    assert (reloaded.title, reloaded.provider_locked, reloaded.model) == ("Bail", True, "m2")
    assert [c.id for c in history.list_conversations()] == [conversation.id]
    with pytest.raises(ValueError, match="champs inconnus"):
        history.update_conversation(conversation.id, inexistant=1)
    history.delete_conversation(conversation.id)
    assert history.get_conversation(conversation.id) is None
    assert history.list_conversations() == []


def test_items_are_archived_without_tool_content(history: History) -> None:
    conversation = history.create_conversation("fake", "m", workdir="/travail")
    turn = _turn(history, conversation.id)
    history.append_item(conversation.id, turn.id, TreeMessage("d1", "arbre"))
    history.append_item(conversation.id, turn.id, UserMessage("Quelle échéance ?"))
    assistant = AssistantMessage("Je lis", "fake", "m", tool_calls=[CALL])
    item_id = history.append_item(conversation.id, turn.id, assistant, provider="fake", model="m")
    history.append_item(
        conversation.id,
        turn.id,
        ToolResultGroup(
            [ToolResult("c1", "read_file", "CONTENU SECRET" * 100, marker="bail.pdf lu (1 400 c.)")]
        ),
    )
    stored = history.items(conversation.id)
    assert [item.kind for item in stored] == ["tree", "user", "assistant", "tool_results"]
    archive = str(stored[3].payload)
    assert "CONTENU SECRET" not in archive and "bail.pdf lu" in archive
    assert stored[2].provider == "fake"

    history.set_sources(
        item_id, [Source("bail.pdf", "/travail", True), Source("x.pdf", None, False)]
    )
    assert [(s.rel_path, s.verified) for s in history.items(conversation.id)[2].sources] == [
        ("bail.pdf", True),
        ("x.pdf", False),
    ]
    pivot = history.pivot_items(conversation.id)
    assert [type(i).__name__ for i in pivot] == [
        "TreeMessage", "UserMessage", "AssistantMessage", "ToolResultGroup"
    ]  # fmt: skip
    group = pivot[3]
    assert isinstance(group, ToolResultGroup)
    assert group.results[0].content == "bail.pdf lu (1 400 c.)"


def test_search_consulted_ignored_and_steps(history: History) -> None:
    first = history.create_conversation("fake", "m", workdir="/travail")
    second = history.create_conversation("fake", "m", workdir="/travail")
    turn = _turn(history, first.id)
    history.append_item(first.id, turn.id, UserMessage("Question sur l'échéance du bail"))
    history.append_item(first.id, turn.id, AssistantMessage("Réponse sur le loyer", "fake", "m"))
    other = _turn(history, second.id)
    history.append_item(second.id, other.id, UserMessage("Question sur les charges"))
    assert [c.id for c in history.search_conversations("échéance")] == [first.id]
    assert [c.id for c in history.search_conversations("loyer")] == [first.id]
    assert history.search_conversations("introuvable") == []
    assert history.search_conversations("  ") == []

    history.add_consulted(first.id, turn.id, "/travail", ["a.pdf", "b.pdf", "a.pdf"])
    history.add_consulted(first.id, turn.id, "/autre", ["c.pdf"])
    assert history.consulted_files(first.id, "/travail") == {"a.pdf", "b.pdf"}
    assert history.consulted_files(first.id, "/autre") == {"c.pdf"}
    assert history.turn_consulted(turn.id) == ["a.pdf", "b.pdf", "c.pdf"]

    history.add_ignored(turn.id, [IgnoredRecord("x.zip", "unsupported_format", "zip")])
    assert history.ignored_files(turn.id)[0].reason == "unsupported_format"
    history.add_step(turn.id, {"kind": "iteration", "index": 1})
    history.add_step(turn.id, {"kind": "tool_call", "tool": "read_file"})
    assert [s["kind"] for s in history.steps(turn.id)] == ["iteration", "tool_call"]

    history.delete_conversation(first.id)
    assert history.search_conversations("échéance") == []


def test_repair_unfinished_turns(history: History) -> None:
    conversation = history.create_conversation("fake", "m", workdir="/travail")
    turn = _turn(history, conversation.id)
    history.append_item(conversation.id, turn.id, UserMessage("Q"))
    history.append_item(
        conversation.id, turn.id, AssistantMessage("Je lis", "fake", "m", tool_calls=[CALL])
    )
    history.add_consulted(conversation.id, turn.id, "/travail", ["a.pdf"])
    assert [t.id for t in history.unfinished_turns()] == [turn.id]

    assert history.repair_unfinished() == [turn.id]
    assert history.unfinished_turns() == []
    stored = history.items(conversation.id)
    assert [item.kind for item in stored] == ["user", "assistant", "tool_results", "interruption"]
    group = stored[2].pivot()
    assert isinstance(group, ToolResultGroup)
    assert group.results[0].call_id == "c1" and group.results[0].synthetic
    assert stored[3].payload["files"] == ["a.pdf"]
    assert history.turns(conversation.id)[0].status == "interrupted"
    # Le message d'interruption redevient une notice dans le contexte du tour suivant.
    pivot = history.pivot_items(conversation.id)
    assert isinstance(pivot[-1], SystemNotice)
    assert history.repair_unfinished() == []


def test_unreadable_item_is_skipped(history: History) -> None:
    conversation = history.create_conversation("fake", "m")
    turn = _turn(history, conversation.id)
    history.append_item(conversation.id, turn.id, UserMessage("Q"))
    with history.db.transaction() as conn:
        conn.execute('UPDATE items SET payload = \'{"type": "user"}\'')
    assert history.pivot_items(conversation.id) == []


# --- budget de contexte --------------------------------------------------------------


def _model(context: int = 10_000, output: int = 1_000) -> ModelInfo:
    return ModelInfo("m", "M", "", context, output, tools=True)


def _history_items(sizes: list[int]) -> list[PivotItem]:
    items: list[PivotItem] = [TreeMessage("d", "arbre " * 50), UserMessage("Question")]
    for index, size in enumerate(sizes):
        call = ToolCall.from_arguments(f"c{index}", "read_file", {"path": f"f{index}.pdf"})
        items.append(AssistantMessage("", "fake", "m", tool_calls=[call]))
        items.append(
            ToolResultGroup(
                [ToolResult(f"c{index}", "read_file", "x" * size, marker=f"f{index}.pdf lu")]
            )
        )
    return items


def test_estimation_and_tree_budget() -> None:
    assert (
        estimate_tokens("") == 0 and estimate_tokens("abcd") == 1 and estimate_tokens("abcde") == 2
    )
    assert item_tokens(UserMessage("x" * 400)) >= 100
    assert tree_budget_chars(_model()) == int(9000 * 0.15) * 4


def test_fit_keeps_everything_when_it_fits() -> None:
    items = _history_items([100, 100])
    fitted, report = fit(items, _model())
    assert fitted == items and report.replaced == 0


def test_fit_replaces_oldest_tool_results() -> None:
    items = _history_items([12_000, 12_000, 400])
    fitted, report = fit(items, _model())
    assert report.replaced == 1
    groups = [i for i in fitted if isinstance(i, ToolResultGroup)]
    assert groups[0].results[0].content == "f0.pdf lu"  # le plus ancien est remplacé
    assert groups[1].results[0].content.startswith("xxx")  # les récents sont conservés
    assert groups[2].results[0].content.startswith("xxx")


def test_fit_keeps_only_the_latest_tree() -> None:
    items: list[PivotItem] = [
        TreeMessage("d1", "ancienne"),
        UserMessage("Q1"),
        TreeMessage("d2", "récente"),
        UserMessage("Q2"),
    ]
    fitted, _ = fit(items, _model())
    trees = [i for i in fitted if isinstance(i, TreeMessage)]
    assert [t.snapshot_digest for t in trees] == ["d2"]


def test_fit_raises_when_conversation_is_saturated() -> None:
    items: list[PivotItem] = [UserMessage("x" * 40_000) for _ in range(3)]
    with pytest.raises(ContextSaturated) as info:
        fit(items, _model())
    assert "nouvelle conversation" in info.value.message()
    # Le prompt système et les définitions d'outils comptent aussi.
    with pytest.raises(ContextSaturated):
        fit([UserMessage("x" * 1000)], _model(context=2000, output=500), overhead_chars=6000)


def test_markers() -> None:
    assert "bail.pdf" in marker_for("read_file", "bail.pdf", "x" * 1400)
    assert "1 400" in marker_for("read_file", "bail.pdf", "x" * 1400).replace("\u202f", " ")
    generic = marker_for("list_files", "", "x" * 10)
    assert "list_files" in generic and "read_file" not in generic


# --- citations -----------------------------------------------------------------------


def test_citation_extraction_and_verification() -> None:
    text = (
        "L'échéance est au 30 juin [[contrats/bail_2022.pdf]], le loyer de 850 € "
        "[[./contrats/bail_2022.pdf]] et les charges [[bureau\\\\budget.xlsx]]. "
        "Voir aussi [[absent.pdf]]."
    )
    assert citations.extract(text) == [
        "contrats/bail_2022.pdf",
        "bureau/budget.xlsx",
        "absent.pdf",
    ]
    sources = citations.verify(text, {"contrats/bail_2022.pdf", "notes.txt"}, "/travail")
    assert [(s.rel_path, s.verified, s.workdir) for s in sources] == [
        ("contrats/bail_2022.pdf", True, "/travail"),
        ("bureau/budget.xlsx", False, "/travail"),
        ("absent.pdf", False, "/travail"),
    ]


def test_citation_edge_cases() -> None:
    assert citations.extract("aucune citation") == []
    assert citations.extract("[[ ]] [[]]") == []
    assert citations.extract("[[a.pdf") == []
    assert citations.extract("[[dossier//fichier .pdf]]") == ["dossier/fichier .pdf"]
    # Le nom en NFD cité par le modèle correspond au fichier consulté en NFC.
    nfd = "e\u0301te\u0301.txt"
    assert citations.verify(f"[[{nfd}]]", {"été.txt"}, None)[0].verified


# --- prompt système ------------------------------------------------------------------


def test_system_prompt_reflects_mode_and_settings() -> None:
    quick = prompts.system_prompt(
        prompts.PromptContext(workdir="/docs", mode="quick", max_iterations=4)
    )
    assert "/docs" in quick and "Rapide" in quick and "4 appels" in quick
    assert "Aucune restriction" in quick
    assert "pas d'OCR" in quick and "mode strict" in quick.lower()
    assert "{" not in quick and "}" not in quick  # tous les gabarits sont remplis

    explore = prompts.system_prompt(
        prompts.PromptContext(
            workdir="/docs",
            mode="explore",
            max_iterations=8,
            scope_rel="contrats",
            general_knowledge=True,
            ocr_available=False,
        )
    )
    assert "Exploration" in explore and "synonymes" in explore
    assert "contrats" in explore and "mixte" in explore.lower()
    assert "Tesseract absent" in explore


def test_notices() -> None:
    assert "/docs" in prompts.workdir_changed("/docs").text
    assert prompts.workdir_changed("/docs").kind == "workdir_changed"
    assert "contrats" in prompts.scope_changed("contrats").text
    assert "retirée" in prompts.scope_changed(None).text
    assert prompts.tree_changed().kind == "tree_changed"
    assert "Exploration" in prompts.mode_changed("explore").text


def test_catalog_models_are_usable_by_the_loop() -> None:
    catalog = load_catalog()
    model = catalog.model("mistral", "mistral-small-latest")
    assert model.usable_context - model.max_output > 0
    assert tree_budget_chars(model) > 10_000


def test_interruption_item_shape() -> None:
    payload = interruption_item("Recherche interrompue.", ["a.pdf"])
    assert payload == {"type": "interruption", "text": "Recherche interrompue.", "files": ["a.pdf"]}


def test_title_from_question() -> None:
    from archipelle.agent.history import title_from_question

    assert title_from_question("  Quel est le   loyer ? ") == "Quel est le loyer ?"
    long_title = title_from_question("mot " * 40)
    assert len(long_title) <= 61 and long_title.endswith("…")
    assert not long_title.endswith(" …")
    assert title_from_question("a" * 200).endswith("…")
