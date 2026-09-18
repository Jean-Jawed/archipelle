from __future__ import annotations

import os
import unicodedata
from pathlib import Path

import pytest

from archipelle.tools import paths
from archipelle.tools.paths import (
    PathRefused,
    Workspace,
    WorkspaceError,
    extended_windows_path,
    is_within,
    same_path_key,
    split_relative,
    to_absolute,
    to_relative,
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "travail"
    (root / "docs" / "sub").mkdir(parents=True)
    (root / "docs" / "a.pdf").write_bytes(b"%PDF")
    (root / "docs" / "sub" / "b.txt").write_text("b", encoding="utf-8")
    (root / "other.txt").write_text("o", encoding="utf-8")
    outside = tmp_path / "dehors"
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    return root


@pytest.fixture
def ws(tree: Path) -> Workspace:
    return Workspace.open(tree)


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("création de liens symboliques non autorisée")


def test_relative_paths_resolve_inside_workspace(ws: Workspace, tree: Path) -> None:
    expected = (tree / "docs" / "a.pdf").resolve()
    assert to_absolute(ws, "docs/a.pdf") == expected
    assert to_absolute(ws, "docs\\a.pdf") == expected
    assert to_absolute(ws, "./docs//a.pdf ") == expected
    assert to_absolute(ws, ".") == ws.root
    assert to_absolute(ws, "") == ws.root


def test_missing_file_is_not_an_error_here(ws: Workspace) -> None:
    assert to_absolute(ws, "docs/nouveau.pdf").name == "nouveau.pdf"


@pytest.mark.parametrize(
    ("rel", "key"),
    [
        ("/etc/passwd", "errors.path.absolute"),
        ("C:/Windows/win.ini", "errors.path.absolute"),
        ("C:\\Windows\\win.ini", "errors.path.absolute"),
        ("\\\\serveur\\partage\\f.txt", "errors.path.absolute"),
        ("docs/../other.txt", "errors.path.parent"),
        ("..", "errors.path.parent"),
        ("docs/..\\..\\x", "errors.path.parent"),
        ("a\x00b", "errors.path.invalid"),
    ],
)
def test_forbidden_paths_are_refused(ws: Workspace, rel: str, key: str) -> None:
    with pytest.raises(PathRefused) as info:
        to_absolute(ws, rel)
    assert info.value.key == key


def test_symlink_pointing_outside_is_refused(ws: Workspace, tree: Path) -> None:
    _symlink(tree / "fuite", tree.parent / "dehors")
    with pytest.raises(PathRefused) as info:
        to_absolute(ws, "fuite/secret.txt")
    assert info.value.key == "errors.path.outside_workspace"


def test_symlink_inside_is_allowed(ws: Workspace, tree: Path) -> None:
    _symlink(tree / "raccourci", tree / "docs")
    assert to_absolute(ws, "raccourci/a.pdf") == (tree / "docs" / "a.pdf").resolve()


def test_symlink_loop_never_escapes(ws: Workspace, tree: Path) -> None:
    _symlink(tree / "boucle1", tree / "boucle2")
    _symlink(tree / "boucle2", tree / "boucle1")
    try:
        result = to_absolute(ws, "boucle1/x.txt")
    except PathRefused:
        return
    assert is_within(result, ws.root)


def test_workspace_root_given_through_symlink(tree: Path, tmp_path: Path) -> None:
    link = tmp_path / "lien_vers_travail"
    _symlink(link, tree)
    ws = Workspace.open(link)
    assert ws.root == tree.resolve()
    assert to_absolute(ws, "other.txt") == (tree / "other.txt").resolve()


def test_scope_restriction(ws: Workspace) -> None:
    scoped = ws.with_scope("docs")
    assert scoped.restricted
    assert scoped.scope_rel == "docs"
    assert to_absolute(scoped, "docs/sub/b.txt").name == "b.txt"
    with pytest.raises(PathRefused) as info:
        to_absolute(scoped, "other.txt")
    assert info.value.key == "errors.path.outside_scope"
    with pytest.raises(PathRefused):
        to_absolute(scoped, ".")
    # L'ouverture des sources ignore la restriction.
    assert to_absolute(scoped, "other.txt", use_scope=False).name == "other.txt"
    unscoped = scoped.with_scope(None)
    assert not unscoped.restricted
    assert unscoped.scope_rel is None


def test_invalid_scopes(ws: Workspace) -> None:
    with pytest.raises(PathRefused):
        ws.with_scope("other.txt")
    with pytest.raises(PathRefused):
        ws.with_scope("../dehors")
    assert not ws.with_scope(".").restricted


def test_workspace_open_errors(tree: Path) -> None:
    with pytest.raises(WorkspaceError):
        Workspace.open("relatif/dossier")
    with pytest.raises(WorkspaceError):
        Workspace.open(tree / "absent")
    with pytest.raises(WorkspaceError):
        Workspace.open(tree / "other.txt")


def test_to_relative(ws: Workspace) -> None:
    assert to_relative(ws, ws.root / "docs" / "sub" / "b.txt") == "docs/sub/b.txt"
    assert to_relative(ws, ws.root) == "."
    with pytest.raises(PathRefused):
        to_relative(ws, ws.root.parent / "dehors" / "secret.txt")


def test_roundtrip(ws: Workspace) -> None:
    for rel in ("docs/a.pdf", "docs/sub/b.txt", "other.txt"):
        assert to_relative(ws, to_absolute(ws, rel)) == rel


def test_nfd_names_on_disk_are_found_with_nfc(tree: Path) -> None:
    nfd_name = unicodedata.normalize("NFD", "été.txt")
    (tree / nfd_name).write_text("x", encoding="utf-8")
    ws = Workspace.open(tree)
    resolved = to_absolute(ws, unicodedata.normalize("NFC", "été.txt"))
    assert resolved.exists()
    rel = to_relative(ws, resolved)
    assert rel == unicodedata.normalize("NFC", "été.txt")
    assert unicodedata.is_normalized("NFC", rel)


def test_case_insensitive_mode(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "CASE_INSENSITIVE", True)
    resolved = to_absolute(ws, "DOCS/A.PDF")
    assert resolved.exists()
    assert is_within(Path(str(ws.root).upper()) / "x", ws.root)
    assert same_path_key("/A/É") == same_path_key(unicodedata.normalize("NFD", "/a/é"))


def test_case_sensitive_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "CASE_INSENSITIVE", False)
    assert not is_within(Path("/Racine/x"), Path("/racine"))
    assert is_within(Path("/racine/x"), Path("/racine"))
    assert not is_within(Path("/racine-bis/x"), Path("/racine"))


def test_split_relative_normalizes_to_nfc() -> None:
    parts = split_relative(unicodedata.normalize("NFD", "dossier/é.txt"))
    assert parts == ["dossier", unicodedata.normalize("NFC", "é.txt")]


def test_extended_windows_paths() -> None:
    short = "C:\\Users\\a\\doc.pdf"
    assert extended_windows_path(short) == short
    long_local = "C:\\" + "\\".join(["dossier"] * 40) + "\\f.pdf"
    assert extended_windows_path(long_local) == "\\\\?\\" + long_local
    long_unc = "\\\\serveur\\partage\\" + "x" * 300
    assert extended_windows_path(long_unc) == "\\\\?\\UNC\\serveur\\partage\\" + "x" * 300
    already = "\\\\?\\" + long_local
    assert extended_windows_path(already) == already


def test_io_path_is_plain_outside_windows(ws: Workspace) -> None:
    if os.name != "nt":
        assert paths.io_path(ws.root) == str(ws.root)
