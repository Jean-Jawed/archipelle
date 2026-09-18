"""Parcours des dossiers et relevé d'arborescence (CDC §3, §5)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from archipelle.persistence.settings_store import ScanExclusions
from archipelle.tools.paths import Workspace
from archipelle.tools.tree import format_size, iter_entries, render, scan
from tests.corpus.make_corpus import Corpus


def _rels(workspace: Workspace, **kwargs: object) -> list[str]:
    return [
        e.rel
        for e in iter_entries(workspace, workspace.root, ScanExclusions.defaults(), **kwargs)  # type: ignore[arg-type]
    ]


def test_exclusions_hide_technical_files(corpus: Corpus) -> None:
    rels = _rels(Workspace.open(corpus.root))
    for hidden in (".git", ".cache_cachee", "node_modules", "Thumbs.db", ".DS_Store"):
        assert not any(r == hidden or r.startswith(hidden + "/") for r in rels), hidden
    assert "bureau/~$rapport.docx" not in rels
    assert "bureau/rapport.docx" in rels
    assert corpus.nfc_rel in rels  # nom NFD renvoyé en NFC


def test_order_depth_and_files_first(corpus: Corpus) -> None:
    workspace = Workspace.open(corpus.root)
    entries = list(iter_entries(workspace, workspace.root, ScanExclusions.defaults(), max_depth=1))
    assert all(e.depth == 1 for e in entries)
    kinds = [e.is_dir for e in entries]
    assert kinds == sorted(kinds)  # fichiers puis dossiers
    deep = list(iter_entries(workspace, workspace.root, ScanExclusions.defaults()))
    index = [e.rel for e in deep].index("contrats")
    assert deep[index + 1].rel.startswith("contrats/") and deep[index + 1].depth == 2


def test_symlinks_are_listed_inside_only_and_never_followed(tmp_path: Path) -> None:
    root = tmp_path / "travail"
    (root / "sous").mkdir(parents=True)
    (root / "sous" / "a.txt").write_text("a")
    outside = tmp_path / "dehors"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    try:
        (root / "vers_dehors").symlink_to(outside, target_is_directory=True)
        (root / "lien_fichier.txt").symlink_to(outside / "secret.txt")
        (root / "boucle").symlink_to(root, target_is_directory=True)
        (root / "vers_sous").symlink_to(root / "sous", target_is_directory=True)
    except OSError:
        pytest.skip("liens symboliques non disponibles")
    rels = _rels(Workspace.open(root))
    assert "vers_dehors" not in rels and "lien_fichier.txt" not in rels
    assert "boucle" in rels and "vers_sous" in rels
    assert not any(r.startswith(("boucle/", "vers_sous/")) for r in rels)


def test_walk_cap(corpus: Corpus) -> None:
    from archipelle.tools.tree import WalkStats

    stats = WalkStats()
    rels = _rels(Workspace.open(corpus.root), max_entries=3, stats=stats)
    assert len(rels) == 3 and stats.capped


def test_unreadable_directory_is_counted(tmp_path: Path) -> None:
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("droits non applicables")
    locked = tmp_path / "verrou"
    locked.mkdir()
    locked.chmod(0)
    try:
        snapshot = scan(Workspace.open(tmp_path), ScanExclusions.defaults())
        assert snapshot.unreadable_dirs == 1
    finally:
        locked.chmod(0o755)


def test_render_full_then_degraded(corpus: Corpus) -> None:
    snapshot = scan(Workspace.open(corpus.root), ScanExclusions.defaults())
    full = render(snapshot, 100_000)
    assert full.level == "full" and not full.truncated
    assert "  bail_2022.pdf (" in full.text and "lot.zip (" in full.text
    assert "format non exploré" in full.text

    by_depth = render(snapshot, len(full.text) - 50)
    assert by_depth.level == "depth:1" and by_depth.truncated
    assert "bail_2022.pdf" not in by_depth.text
    assert "contrats/ (contenu non affiché — fichiers : 5, sous-dossiers : 0)" in by_depth.text
    assert "list_files" in by_depth.text

    summary = render(snapshot, 700)
    assert summary.level == "summary" and summary.truncated
    assert len(summary.text) <= 700
    assert "contrats/ — fichiers : 5 (pdf 5)" in summary.text

    tiny = render(snapshot, 300)
    assert len(tiny.text) <= 300 and tiny.truncated


def test_digest_changes_with_content(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    workspace = Workspace.open(tmp_path)
    before = scan(workspace, ScanExclusions.defaults())
    assert scan(workspace, ScanExclusions.defaults()).digest == before.digest
    (tmp_path / "b.txt").write_text("b")
    after = scan(workspace, ScanExclusions.defaults())
    assert after.digest != before.digest and after.file_count == 2


def test_empty_folder(tmp_path: Path) -> None:
    rendered = render(scan(Workspace.open(tmp_path), ScanExclusions.defaults()), 1000)
    assert "dossier vide" in rendered.text


def test_format_size() -> None:
    assert format_size(512) == "512 o"
    assert format_size(1024) == "1 Ko"
    assert format_size(1536) == "1,5 Ko"
    assert format_size(5 * 1024**3) == "5 Go"
