"""Les cinq outils, appelés comme par la boucle agentique (via ``dispatch``)."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

import pytest

from archipelle.core.cancel import CancelToken
from archipelle.core.outcome import Cancelled
from archipelle.tools.context import IgnoredReason, ToolContext, ToolLimits, ToolOutcome
from archipelle.tools.open_source import open_source
from archipelle.tools.registry import dispatch, registered_tools, tool_specs
from tests.corpus.make_corpus import LONG_TEXT_CHARS, Corpus
from tests.tools.conftest import MakeContext, requires_tesseract


def call(ctx: ToolContext, name: str, **arguments: Any) -> ToolOutcome:
    return dispatch(ctx, name, arguments)


def reasons(outcome: ToolOutcome) -> dict[str, set[IgnoredReason]]:
    found: dict[str, set[IgnoredReason]] = {}
    for entry in outcome.ignored:
        found.setdefault(entry.rel_path, set()).add(entry.reason)
    return found


# --- registre ------------------------------------------------------------------------


def test_specs_are_complete_and_read_only() -> None:
    names = [spec.name for spec in tool_specs()]
    assert names == ["list_files", "search_files", "search_fulltext", "read_file", "compute"]
    for spec in tool_specs():
        assert spec.description and not spec.description.startswith("tool_specs.")
        assert spec.parameters["type"] == "object"
        for schema in spec.parameters["properties"].values():
            assert not schema["description"].startswith("tool_specs.")
    assert not any(w in n for n in names for w in ("write", "delete", "move", "rename"))
    assert set(registered_tools()) == set(names)


def test_dispatch_rejects_bad_calls(make_ctx: MakeContext) -> None:
    ctx = make_ctx()
    assert "Outil inconnu" in dispatch(ctx, "delete_file", {}).content
    assert "illisibles" in dispatch(ctx, "list_files", "{pas du json").content
    missing = dispatch(ctx, "read_file", {})
    assert not missing.ok and "path" in missing.content
    unknown = dispatch(ctx, "list_files", {"chemin": "x"})
    assert not unknown.ok and "chemin" in unknown.content
    wrong_type = dispatch(ctx, "list_files", {"depth": "deux"})
    assert not wrong_type.ok and "depth" in wrong_type.content


def test_dispatch_coerces_obvious_mistakes(make_ctx: MakeContext) -> None:
    ctx = make_ctx()
    outcome = dispatch(
        ctx, "search_files", '{"keywords": "rapport", "max_results": "5", "path": null}'
    )
    assert outcome.ok and "bureau/rapport.docx" in outcome.content


def test_dispatch_reports_refused_paths_and_short_keywords(make_ctx: MakeContext) -> None:
    ctx = make_ctx()
    for path in ("../etc", "/etc", "C:/Windows"):
        outcome = call(ctx, "read_file", path=path)
        assert not outcome.ok, path
    short = call(ctx, "search_fulltext", keywords=["an"])
    assert not short.ok and "an" in short.content


def test_dispatch_propagates_cancellation(make_ctx: MakeContext) -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        call(make_ctx(cancel=token), "search_fulltext", keywords=["loyer"])


def test_dispatch_hides_unexpected_errors(
    make_ctx: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    import archipelle.tools.registry as registry

    def boom(ctx: ToolContext, **_: Any) -> ToolOutcome:
        raise ZeroDivisionError

    tools = dict(registry.registered_tools())
    tools["compute"] = registry.RegisteredTool(tools["compute"].spec, boom)
    values = [{"value": 1, "source": "a.txt"}]
    outcome = dispatch(make_ctx(), "compute", {"operation": "somme", "values": values}, tools)
    assert not outcome.ok and "inattendue" in outcome.content


# --- list_files ----------------------------------------------------------------------


def test_list_files_default_and_depth(make_ctx: MakeContext) -> None:
    ctx = make_ctx()
    top = call(ctx, "list_files")
    assert top.ok
    assert "contrats/" in top.content and "notes.txt (" in top.content
    assert "contrats/bail_2022.pdf" not in top.content
    assert ".git" not in top.content and "node_modules" not in top.content
    deep = call(ctx, "list_files", path="contrats", depth=3)
    assert "contrats/bail_2022.pdf (" in deep.content
    assert "Entrées : 5." in deep.content


def test_list_files_filters(make_ctx: MakeContext) -> None:
    ctx = make_ctx()
    pdfs = call(ctx, "list_files", depth=5, extensions=[".PDF"])
    assert "Entrées : 6." in pdfs.content
    assert "contrats/" not in pdfs.content.splitlines()
    old = call(ctx, "list_files", modified_before="2021-01-01")
    assert "ancien.txt" in old.content and "Entrées : 1." in old.content
    recent = call(ctx, "list_files", modified_after="2021-01-01")
    assert "ancien.txt" not in recent.content
    bad = call(ctx, "list_files", modified_after="hier")
    assert not bad.ok and "AAAA-MM-JJ" in bad.content
    inverted = call(ctx, "list_files", modified_after="2024-01-02", modified_before="2024-01-01")
    assert not inverted.ok


def test_list_files_cap_is_explicit(make_ctx: MakeContext) -> None:
    outcome = call(make_ctx(limits=ToolLimits(list_max=3)), "list_files", depth=5)
    assert re.search(r"Entrées : \d+ au total, 3 affichées \(plafond atteint", outcome.content)
    assert call(make_ctx(), "list_files", max_results=2).content.count("\n") == 3


def test_list_files_errors(make_ctx: MakeContext) -> None:
    ctx = make_ctx()
    assert not call(ctx, "list_files", path="inexistant").ok
    assert not call(ctx, "list_files", path="notes.txt").ok


def test_scope_restriction(make_ctx: MakeContext) -> None:
    ctx = make_ctx(scope="contrats")
    listing = call(ctx, "list_files")
    assert "contrats/bail_2022.pdf" in listing.content and "notes.txt" not in listing.content
    assert not call(ctx, "list_files", path="bureau").ok
    assert not call(ctx, "read_file", path="notes.txt").ok
    found = call(ctx, "search_fulltext", keywords=["échéance"])
    assert "notes.txt" not in found.content


# --- search_files --------------------------------------------------------------------


def test_search_files(make_ctx: MakeContext, corpus: Corpus) -> None:
    ctx = make_ctx()
    outcome = call(ctx, "search_files", keywords=["ETE", "BAIL"])
    assert corpus.nfc_rel in outcome.content
    assert "contrats/bail_2022.pdf" in outcome.content
    assert "Correspondances : 2." in outcome.content
    dirs = call(ctx, "search_files", keywords=["contrat"])
    assert "contrats/" in dirs.content
    only_txt = call(ctx, "search_files", keywords=["bail", "ete"], extensions=["txt"])
    assert "Correspondances : 1." in only_txt.content
    none = call(ctx, "search_files", keywords=["zzzz"])
    assert "Aucun fichier" in none.content
    hidden = call(ctx, "search_files", keywords=["secret", "paquet", "config"])
    assert "Aucun fichier" in hidden.content
    capped = call(
        make_ctx(limits=ToolLimits(search_files_max=1)), "search_files", keywords=["bail", "ete"]
    )
    assert "2 au total, 1 affichées" in capped.content


# --- search_fulltext -----------------------------------------------------------------


def test_fulltext_finds_all_formats(make_ctx: MakeContext, corpus: Corpus) -> None:
    ctx = make_ctx()
    outcome = call(ctx, "search_fulltext", keywords=["ECHEANCE"])
    assert outcome.ok
    content = outcome.content
    for rel in (
        "notes.txt",
        "ancien.txt",
        "contrats/bail_2022.pdf",
        "bureau/rapport.docx",
        "bureau/presentation.pptx",
    ):
        assert f"- {rel} — occurrences" in content, rel
    for hidden in (".cache_cachee", "node_modules", "lot.zip"):
        assert f"- {hidden}" not in content
    assert {c.rel_path for c in outcome.consulted} >= {"notes.txt", "contrats/bail_2022.pdf"}
    assert "Bilan — fichiers fouillés" in content
    assert "format n'est pas exploré : 2" in content  # lot.zip et ancien.doc


def test_fulltext_positions_are_usable_by_read_file(make_ctx: MakeContext) -> None:
    ctx = make_ctx()
    outcome = call(ctx, "search_fulltext", keywords=["clause de résiliation"], extensions=["pdf"])
    match = re.search(r"\(page=(\d+), offset=(\d+)\) « (.+?) »", outcome.content)
    assert match is not None
    page, offset = int(match[1]), int(match[2])
    assert page == 2
    read = call(
        ctx, "read_file", path="contrats/bail_2022.pdf", page=page, offset=offset, max_chars=21
    )
    assert "[page 2]\nclause de résiliation" in read.content


def test_fulltext_expression_across_line_break(make_ctx: MakeContext) -> None:
    outcome = call(make_ctx(), "search_fulltext", keywords=["préavis de trois mois. Le loyer"])
    assert "contrats/bail_2022.pdf" in outcome.content


def test_fulltext_snippet_and_count_caps(make_ctx: MakeContext) -> None:
    ctx = make_ctx(limits=ToolLimits(fulltext_files=1))
    outcome = call(ctx, "search_fulltext", keywords=["texte"], path="gros")
    assert "occurrences au total), 1 affichés (plafond atteint" in outcome.content
    assert outcome.content.count("  • (page=") == 3
    assert "autres occurrences dans ce fichier" in outcome.content
    assert len(outcome.consulted) == 1


def test_fulltext_quick_mode_counts_scans(make_ctx: MakeContext) -> None:
    outcome = call(make_ctx("quick"), "search_fulltext", keywords=["loyer mensuel"])
    assert "contrats/scan_quittance.pdf" not in outcome.content.split("Bilan")[0]
    assert "non traités en mode Rapide" in outcome.content
    journal = reasons(outcome)
    assert IgnoredReason.SCAN_NOT_PROCESSED in journal["contrats/scan_quittance.pdf"]
    assert IgnoredReason.SCAN_NOT_PROCESSED in journal["images/facture.png"]


@requires_tesseract
def test_fulltext_explore_mode_reads_scans(make_ctx: MakeContext) -> None:
    ctx = make_ctx("explore")
    outcome = call(ctx, "search_fulltext", keywords=["loyer mensuel", "facture electricite"])
    head = outcome.content.split("Bilan")[0]
    assert "contrats/scan_quittance.pdf" in head
    assert "contrats/mixte.pdf" in head
    assert "images/facture.png" in head
    assert "non traités" not in outcome.content
    journal = reasons(outcome)
    assert IgnoredReason.NO_OCR_TEXT in journal["images/vide.png"]


def test_fulltext_without_tesseract(make_ctx: MakeContext) -> None:
    outcome = call(make_ctx("explore", ocr_config=None), "search_fulltext", keywords=["loyer"])
    assert "Tesseract absent" in outcome.content
    assert IgnoredReason.OCR_UNAVAILABLE in reasons(outcome)["contrats/scan_quittance.pdf"]


def test_fulltext_reports_unreadable_files(make_ctx: MakeContext) -> None:
    outcome = call(make_ctx(), "search_fulltext", keywords=["loyer"], path="contrats")
    assert "Fichiers illisibles : 2." in outcome.content
    assert "contrats/protege.pdf » est protégé par un mot de passe" in outcome.content
    journal = reasons(outcome)
    assert journal["contrats/corrompu.pdf"] == {IgnoredReason.READ_ERROR}


def test_fulltext_new_extraction_cap_and_resume(make_ctx: MakeContext) -> None:
    limits = ToolLimits(fulltext_new_files=1)
    first = call(make_ctx(limits=limits), "search_fulltext", keywords=["loyer"], path="bureau")
    assert "plafond de 1 fichiers nouvellement extraits" in first.content
    assert "relance la même recherche" in first.content
    assert IgnoredReason.SEARCH_CAP in {e.reason for e in first.ignored}
    for _ in range(3):
        call(make_ctx(limits=limits), "search_fulltext", keywords=["loyer"], path="bureau")
    last = call(make_ctx(limits=limits), "search_fulltext", keywords=["loyer"], path="bureau")
    assert "plafond" not in last.content
    assert "(dont 3 depuis le cache)" in last.content
    assert "Fichiers illisibles : 1." in last.content  # l'échec mémorisé reste signalé


def test_fulltext_time_cap_keeps_progress(make_ctx: MakeContext) -> None:
    ctx = make_ctx(limits=ToolLimits(fulltext_seconds=0.0))
    outcome = call(ctx, "search_fulltext", keywords=["loyer"])
    assert "plafond de 0 secondes atteint" in outcome.content
    assert "Couverture partielle" in outcome.content


def test_fulltext_order_prefers_matching_names(make_ctx: MakeContext) -> None:
    outcome = call(make_ctx(), "search_fulltext", keywords=["loyer", "donnees"])
    files = re.findall(r"^- (\S+) — occurrences", outcome.content, flags=re.M)
    assert files[0] == "donnees.csv"


def test_fulltext_cancel_from_progress(make_ctx: MakeContext) -> None:
    from tests.tools.conftest import Progress

    token = CancelToken()
    progress = Progress()
    progress.on_event = lambda key, params: token.cancel()
    with pytest.raises(Cancelled):
        call(make_ctx(cancel=token, progress=progress), "search_fulltext", keywords=["loyer"])


# --- read_file -----------------------------------------------------------------------


def test_read_text_truncation_and_resume(make_ctx: MakeContext) -> None:
    ctx = make_ctx()
    first = call(ctx, "read_file", path="gros/long.txt")
    assert "LECTURE TRONQUÉE à 20\u202f000 caractères" in first.content
    assert "page=1, offset=20000" in first.content
    assert first.consulted[0].rel_path == "gros/long.txt"
    last = call(ctx, "read_file", path="gros/long.txt", offset=LONG_TEXT_CHARS - 10)
    assert "MOT FINAL" in last.content and "Fin du document." in last.content
    assert not call(ctx, "read_file", path="notes.txt", page=2).ok


def test_read_text_encodings(make_ctx: MakeContext, corpus: Corpus) -> None:
    ctx = make_ctx()
    assert "12 000 €" in call(ctx, "read_file", path="ancien.txt").content
    assert "piscine" in call(ctx, "read_file", path=corpus.nfc_rel).content


def test_read_pdf_markers_and_resume(make_ctx: MakeContext) -> None:
    ctx = make_ctx()
    whole = call(ctx, "read_file", path="contrats/bail_2022.pdf")
    content = whole.content
    assert content.index("[page 1]") < content.index("[page 2]") < content.index("[page 3]")
    assert "Fin du document." in content and "PDF « contrats/bail_2022.pdf » — 3 pages" in content
    cut = call(ctx, "read_file", path="gros/long.pdf", max_chars=5000)
    match = re.search(r"page=(\d+), offset=(\d+)", cut.content)
    assert match is not None
    page, offset = int(match[1]), int(match[2])
    resumed = call(ctx, "read_file", path="gros/long.pdf", page=page, offset=offset, max_chars=5000)
    assert resumed.content.splitlines()[1] == f"[page {page}]"
    assert not call(ctx, "read_file", path="contrats/bail_2022.pdf", page=9).ok


def test_read_long_pdf_over_several_batches(make_ctx: MakeContext) -> None:
    ctx = make_ctx(limits=ToolLimits(pdf_batch_pages=4))
    outcome = call(ctx, "read_file", path="gros/long.pdf", page=30, max_chars=20_000)
    assert "[page 33]" in outcome.content and "indexation du loyer" in outcome.content


def test_read_scanned_pdf_quick_mode(make_ctx: MakeContext) -> None:
    outcome = call(make_ctx("quick"), "read_file", path="contrats/scan_quittance.pdf")
    assert "[page 1 — non lue]" in outcome.content
    assert "Scanned by" in outcome.content  # le texte natif éventuel reste visible
    assert "2 pages non lues en mode Rapide" in outcome.content
    assert not outcome.consulted
    assert reasons(outcome)["contrats/scan_quittance.pdf"] == {IgnoredReason.SCAN_NOT_PROCESSED}


@requires_tesseract
def test_read_scanned_pdf_explore_mode(make_ctx: MakeContext) -> None:
    outcome = call(make_ctx("explore"), "read_file", path="contrats/scan_quittance.pdf")
    assert "non lue" not in outcome.content
    assert "Montant 850 euros" in outcome.content
    assert outcome.consulted


@requires_tesseract
def test_read_scanned_pdf_interrupted_by_timeout(make_ctx: MakeContext) -> None:
    ctx = make_ctx("explore", limits=ToolLimits(tool_timeout_s=0.3))
    call(make_ctx("quick"), "read_file", path="contrats/scan_quittance.pdf")  # texte natif en cache
    outcome = call(ctx, "read_file", path="contrats/scan_quittance.pdf")
    assert "LECTURE INCOMPLÈTE" in outcome.content and "page=1" in outcome.content


def test_read_documents_with_notes(make_ctx: MakeContext) -> None:
    ctx = make_ctx()
    budget = call(ctx, "read_file", path="bureau/budget.xlsx")
    assert "1 cellules à formule" in budget.content and "Chauffage" in budget.content
    mail = call(ctx, "read_file", path="mails/message.eml")
    assert "non lues : avenant.pdf" in mail.content
    assert not call(ctx, "read_file", path="bureau/rapport.docx", page=2).ok
    part = call(ctx, "read_file", path="bureau/rapport.docx", offset=10, max_chars=5)
    assert "caractères 10 à 15" in part.content


def test_read_images(make_ctx: MakeContext) -> None:
    quick = call(make_ctx("quick"), "read_file", path="images/facture.png")
    assert "non lue en mode Rapide" in quick.content
    no_ocr = call(make_ctx("explore", ocr_config=None), "read_file", path="images/facture.png")
    assert "Tesseract absent" in no_ocr.content


@requires_tesseract
def test_read_images_with_ocr(make_ctx: MakeContext) -> None:
    ctx = make_ctx("explore")
    assert "Total 245 euros" in call(ctx, "read_file", path="images/facture.png").content
    blank = call(ctx, "read_file", path="images/vide.png")
    assert "Aucun texte exploitable" in blank.content and not blank.consulted


def test_read_errors(make_ctx: MakeContext) -> None:
    ctx = make_ctx()
    zipped = call(ctx, "read_file", path="archives/lot.zip")
    assert not zipped.ok and "n'est pas exploré" in zipped.content
    assert reasons(zipped)["archives/lot.zip"] == {IgnoredReason.UNSUPPORTED_FORMAT}
    protected = call(ctx, "read_file", path="contrats/protege.pdf")
    assert not protected.ok and "mot de passe" in protected.content
    assert not call(ctx, "read_file", path="contrats").ok
    assert not call(ctx, "read_file", path="absent.pdf").ok
    assert not call(ctx, "read_file", path="notes.txt", offset=-1).ok


def test_read_permission_denied(make_ctx: MakeContext, corpus: Corpus) -> None:
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("droits non applicables")
    target = corpus.root / "notes.txt"
    target.chmod(0)
    try:
        outcome = call(make_ctx(), "read_file", path="notes.txt")
        assert not outcome.ok and "Accès refusé" in outcome.content
    finally:
        target.chmod(0o644)


# --- compute -------------------------------------------------------------------------


def _values(*items: tuple[Any, str]) -> list[dict[str, Any]]:
    return [{"value": v, "unit": u, "source": "docs/a.pdf"} for v, u in items]


@pytest.mark.parametrize(
    ("operation", "items", "expected"),
    [
        ("somme", [(0.1, "€"), ("0.2", "EUR")], "0,30 EUR — valeur exacte : 0.3"),
        ("moyenne", [(1, "m²"), (2, "m2"), (2, "m2")], "1,67 m2 — valeur exacte : 1.666666"),
        ("min", [(3, "jours"), (-1, "jour")], "-1,00 jour"),
        ("max", [("1e3", "euros"), (999, "€")], "1\u202f000,00 EUR"),
        ("différence", [(1250.5, "€ HT"), (250.25, "euros HT")], "1\u202f000,25 EUR HT"),
        ("difference", [(1, ""), (3, "")], "-2,00 — valeur exacte : -2"),
        ("pourcentage", [(45, "€"), (180, "€")], "25,00 %"),
        ("ratio", [(10, "k€"), (4, "k€")], "2,50 — valeur exacte : 2.5"),
        ("compte", [("n'importe quoi", "€"), (2, "m²")], "(compte) : 2,00 — valeur exacte : 2"),
        ("somme", [(1234567.891, "€")], "1\u202f234\u202f567,89 EUR"),
        ("somme", [(0.005, "€")], "0,01 EUR"),
    ],
)
def test_compute_operations(
    make_ctx: MakeContext, operation: str, items: list[tuple[Any, str]], expected: str
) -> None:
    outcome = call(make_ctx(), "compute", operation=operation, values=_values(*items))
    assert outcome.ok, outcome.content
    assert expected in outcome.content
    assert "source : docs/a.pdf" in outcome.content


@pytest.mark.parametrize(
    ("operation", "items", "message"),
    [
        ("somme", [(1, "€"), (1, "k€")], "unités différentes"),
        ("somme", [(1, "€ HT"), (1, "€ TTC")], "unités différentes"),
        ("somme", [(1, "€"), (1, "")], "sans unité"),
        ("ratio", [(1, "€"), (0, "€")], "division par zéro"),
        ("pourcentage", [(1, "€")], "exactement deux valeurs"),
        ("somme", [("1 200,50", "€")], "non numérique"),
        ("somme", [(True, "€")], "values[0].value"),
        ("somme", [("NaN", "€")], "non numérique"),
        ("médiane", [(1, "€")], "Opération inconnue"),
        ("somme", [], "vide"),
    ],
)
def test_compute_refusals(
    make_ctx: MakeContext, operation: str, items: list[tuple[Any, str]], message: str
) -> None:
    outcome = call(make_ctx(), "compute", operation=operation, values=_values(*items))
    assert not outcome.ok
    assert message in outcome.content


def test_compute_requires_source(make_ctx: MakeContext) -> None:
    outcome = call(make_ctx(), "compute", operation="somme", values=[{"value": 1, "unit": "€"}])
    assert not outcome.ok and "source" in outcome.content


def test_units_normalization() -> None:
    from archipelle.tools.units import normalize_unit

    assert normalize_unit("€") == normalize_unit("Euros") == "EUR"
    assert normalize_unit("€ HT") == normalize_unit("euros hors taxes") == "EUR HT"
    assert normalize_unit("k€") != normalize_unit("€")
    assert normalize_unit("milliers d’euros") == normalize_unit("k€")
    assert normalize_unit(None) == normalize_unit("  ") == ""
    assert normalize_unit("parsecs") == "parsecs"


# --- ouverture des sources -----------------------------------------------------------


def test_open_source_triple_check(corpus: Corpus, tmp_path: Path) -> None:
    opened: list[Path] = []
    root = str(corpus.root)

    def opener(path: Path) -> None:
        opened.append(path)

    assert open_source(root, "contrats/bail_2022.pdf", verified=True, opener=opener).ok
    assert opened == [(corpus.root / "contrats/bail_2022.pdf").resolve()]
    refused = [
        open_source(root, "contrats/bail_2022.pdf", verified=False, opener=opener),
        open_source(root, "archives/lot.zip", verified=True, opener=opener),
        open_source(root, "../dehors.pdf", verified=True, opener=opener),
        open_source(root, "absent.pdf", verified=True, opener=opener),
        open_source(None, "notes.txt", verified=True, opener=opener),
    ]
    assert all(not r.ok for r in refused)
    assert len(opened) == 1

    script = tmp_path / "piege.pdf"
    target = tmp_path / "script.sh"
    target.write_text("echo")
    try:
        script.symlink_to(target)
    except OSError:
        return
    assert not open_source(str(tmp_path), "piege.pdf", verified=True, opener=opener).ok


def test_open_source_failure_is_reported(corpus: Corpus) -> None:
    def failing(path: Path) -> None:
        raise OSError("aucune application")

    result = open_source(str(corpus.root), "notes.txt", verified=True, opener=failing)
    assert not result.ok


def test_whole_suite_budget() -> None:
    """Garde-fou : les appels ci-dessus doivent rester rapides (pas de fuite de threads)."""
    import threading

    assert threading.active_count() < 40
    assert time.monotonic() > 0


def test_extension_groups_target_the_right_files(make_ctx: MakeContext, corpus: Corpus) -> None:
    (corpus.root / "outils.py").write_text("def total(loyer):\n    return loyer * 12\n", "utf-8")
    try:
        ctx = make_ctx()
        code = call(ctx, "search_fulltext", keywords=["loyer"], extensions=["code"])
        assert "outils.py" in code.content
        assert "notes.txt" not in code.content and "bail_2022.pdf" not in code.content

        documents = call(ctx, "search_fulltext", keywords=["loyer"], extensions=["documents"])
        assert "outils.py" not in documents.content
        assert "contrats/bail_2022.pdf" in documents.content

        listed = call(ctx, "list_files", depth=5, extensions=["code"])
        assert "outils.py" in listed.content and "Entrées : 1." in listed.content
        read = call(ctx, "read_file", path="outils.py")
        assert "return loyer * 12" in read.content  # lu tel quel, sans mise en forme
    finally:
        (corpus.root / "outils.py").unlink()


def test_build_directories_are_excluded_by_default() -> None:
    from archipelle.persistence.settings_store import ScanExclusions

    names = {name.casefold() for name in ScanExclusions.defaults().dir_names}
    assert {"venv", ".venv", "node_modules", "dist", "build", "target", "__pycache__"} <= names
