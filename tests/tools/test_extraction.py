"""Extracteurs appelés directement (hors sous-processus)."""

from __future__ import annotations

from pathlib import Path

import pytest

from archipelle.extraction import images, mail_html, office, pdf, tasks, text_files
from archipelle.extraction.formats import FormatKind, kind_of, normalize_extension
from archipelle.extraction.types import DocumentNote, ErrorCode, ExtractionError, ExtractionMethod
from tests.corpus.make_corpus import IMAGE_LINES, Corpus
from tests.tools.conftest import OCR_CONFIG, requires_tesseract


def test_formats() -> None:
    assert kind_of("a/B.PDF") is FormatKind.PDF
    assert kind_of("x.tiff") is FormatKind.IMAGE
    assert kind_of("x.zip") is None
    assert kind_of("sans_extension") is None
    assert normalize_extension("*.DOCX") == "docx"


def test_text_decoding_fallbacks(corpus: Corpus) -> None:
    assert text_files.decode_bytes("\ufeffé".encode()) == "é"
    assert "€" in text_files.read_text(str(corpus.root / "ancien.txt"))
    assert "\r" not in text_files.read_text(str(corpus.root / "ancien.txt"))
    damaged = text_files.read_text(str(corpus.root / "abime.txt"))
    assert "\ufffd" in damaged and "quittance" in damaged


def test_text_slice(corpus: Corpus) -> None:
    piece = text_files.read_slice(str(corpus.root / "notes.txt"), 5, 10)
    assert piece.offset == 5 and len(piece.text) == 10
    assert piece.total_chars > 15
    beyond = text_files.read_slice(str(corpus.root / "notes.txt"), 10_000, 10)
    assert beyond.text == "" and beyond.offset == beyond.total_chars


def test_pdf_native_and_scan_detection(corpus: Corpus) -> None:
    bail = pdf.native_pages(str(corpus.root / "contrats/bail_2022.pdf"), 1, 99)
    assert [p.method for p in bail] == [ExtractionMethod.NATIVE] * 3
    assert "résiliation" in bail[1].text
    scan = pdf.native_pages(str(corpus.root / "contrats/scan_quittance.pdf"), 1, 2)
    # Page 1 : pied de page natif de plus de 50 caractères, mais image pleine page.
    assert len("".join(scan[0].text.split())) > 50
    assert [p.method for p in scan] == [ExtractionMethod.PENDING] * 2
    mixed = pdf.native_pages(str(corpus.root / "contrats/mixte.pdf"), 1, 2)
    assert [p.method for p in mixed] == [ExtractionMethod.NATIVE, ExtractionMethod.PENDING]


def test_scan_rules() -> None:
    assert pdf.is_scanned("x" * 49, 0.0)
    assert not pdf.is_scanned("x" * 50, 0.0)
    assert pdf.is_scanned("x" * 299, 0.81)
    assert not pdf.is_scanned("x" * 300, 0.95)
    assert not pdf.is_scanned("x" * 299, 0.80)


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("contrats/protege.pdf", ErrorCode.PASSWORD),
        ("contrats/corrompu.pdf", ErrorCode.CORRUPT),
        ("contrats/absent.pdf", ErrorCode.NOT_FOUND),
    ],
)
def test_pdf_errors(corpus: Corpus, name: str, code: ErrorCode) -> None:
    with pytest.raises(ExtractionError) as info:
        pdf.page_count(str(corpus.root / name))
    assert info.value.code is code


def test_extraction_error_is_picklable() -> None:
    import pickle

    error = pickle.loads(pickle.dumps(ExtractionError(ErrorCode.PASSWORD, "détail")))
    assert error.code is ErrorCode.PASSWORD and error.detail == "détail"


def test_docx(corpus: Corpus) -> None:
    text = office.extract_docx(str(corpus.root / "bureau/rapport.docx")).text
    lines = text.splitlines()
    assert lines[:2] == ["[En-tête]", "En-tête confidentiel"]
    assert lines.index("Premier paragraphe du rapport.") < lines.index("Échéance | 30/06/2025")
    assert lines.index("Échéance | 30/06/2025") < lines.index("Paragraphe après le tableau.")
    assert lines[-1] == "Pied de page société"


def test_protected_office_file(corpus: Corpus) -> None:
    with pytest.raises(ExtractionError) as info:
        office.extract_docx(str(corpus.root / "bureau/protege.docx"))
    assert info.value.code is ErrorCode.PASSWORD


def test_xlsx_values_and_missing_formula_results(corpus: Corpus) -> None:
    document = office.extract_xlsx(str(corpus.root / "bureau/budget.xlsx"))
    assert "[Feuille : Budget]" in document.text
    assert "Chauffage\t1200.5" in document.text
    assert "[Feuille : Notes]" in document.text
    assert "=SUM" not in document.text
    [note] = document.notes
    assert note.note is DocumentNote.XLSX_MISSING_VALUES and note.detail == "1"


def test_pptx(corpus: Corpus) -> None:
    text = office.extract_pptx(str(corpus.root / "bureau/presentation.pptx")).text
    assert "[Diapositive 1]" in text and "[Diapositive 2]" in text
    assert "insister sur l'échéance" in text
    assert "Étude | 3 000 €" in text


def test_eml(corpus: Corpus) -> None:
    document = mail_html.extract_eml(str(corpus.root / "mails/message.eml"))
    assert "Objet : Révision du loyer" in document.text
    assert "révisé au 1er avril" in document.text
    assert document.notes[0].detail == "avenant.pdf"


def test_html(corpus: Corpus) -> None:
    text = mail_html.extract_html(str(corpus.root / "web/page.html")).text
    assert text.splitlines()[0] == "Guide du locataire"
    assert "dépôt de garantie" in text and "Caution | 1 mois" in text
    assert "motsecret" not in text and "color" not in text


def test_html_declared_charset() -> None:
    markup = '<meta charset="iso-8859-1"><p>d\xe9p\xf4t</p>'.encode("latin-1")
    assert text_files.decode_bytes(markup, "iso-8859-1").endswith("dépôt</p>")


def test_unsupported_document_task(corpus: Corpus) -> None:
    with pytest.raises(ExtractionError) as info:
        tasks.extract_document(str(corpus.root / "archives/lot.zip"))
    assert info.value.code is ErrorCode.UNSUPPORTED


def test_task_wraps_unexpected_errors(corpus: Corpus) -> None:
    with pytest.raises(ExtractionError) as info:
        tasks.extract_document(str(corpus.root / "notes.txt").replace(".txt", ".docx"))
    assert info.value.code is ErrorCode.NOT_FOUND


@requires_tesseract
def test_image_ocr(corpus: Corpus) -> None:
    assert OCR_CONFIG is not None
    document = images.ocr_image(str(corpus.root / "images/facture.png"), OCR_CONFIG)
    assert document.method is ExtractionMethod.OCR
    assert IMAGE_LINES[0] in document.text
    blank = images.ocr_image(str(corpus.root / "images/vide.png"), OCR_CONFIG)
    assert blank.notes[0].note is DocumentNote.NO_TEXT
    with pytest.raises(ExtractionError) as info:
        images.ocr_image(str(corpus.root / "images/casse.jpg"), OCR_CONFIG)
    assert info.value.code is ErrorCode.CORRUPT


@requires_tesseract
def test_pdf_page_ocr(corpus: Corpus) -> None:
    assert OCR_CONFIG is not None
    page = pdf.ocr_page(str(corpus.root / "contrats/scan_quittance.pdf"), 1, OCR_CONFIG)
    assert page.method is ExtractionMethod.OCR
    assert "LOYER MENSUEL" in page.text


def test_code_files_are_read_as_plain_text() -> None:
    from archipelle.extraction.formats import EXTENSION_GROUPS, expand_extensions

    assert kind_of("app.py") is FormatKind.TEXT
    assert kind_of("Bouton.tsx") is FormatKind.TEXT
    assert kind_of("config.YAML") is FormatKind.TEXT
    # Une page web reste dépouillée de ses balises : c'est l'usage le plus courant.
    assert kind_of("page.html") is FormatKind.DOCUMENT
    # Toujours hors périmètre.
    assert kind_of("archive.zip") is None and kind_of("binaire.exe") is None

    assert expand_extensions(["code"]) == EXTENSION_GROUPS["code"]
    assert expand_extensions(["documents"]) >= {"pdf", "docx", "txt"}
    assert expand_extensions(["images"]) == {"png", "jpg", "jpeg", "tiff", "tif"}
    assert expand_extensions(["code", ".PDF"]) == EXTENSION_GROUPS["code"] | {"pdf"}
    assert expand_extensions(["groupe-inconnu"]) == {"groupe-inconnu"}
    assert expand_extensions([]) == set() and expand_extensions(["  "]) == set()


def test_executable_code_files_are_read_but_never_opened(tmp_path: Path) -> None:
    """Lisible n'est pas ouvrable : un double-clic exécuterait ces fichiers (CDC §9)."""
    from archipelle.extraction.formats import OPENABLE_EXTENSIONS
    from archipelle.tools.open_source import open_source

    for name in ("app.py", "script.bat", "outil.sh", "widget.js", "notes.md", "data.json"):
        (tmp_path / name).write_text("contenu", encoding="utf-8")
    opened: list[Path] = []

    for refused in ("app.py", "script.bat", "outil.sh", "widget.js"):
        assert kind_of(refused) is FormatKind.TEXT  # bien lu par les outils
        assert not open_source(str(tmp_path), refused, verified=True, opener=opened.append).ok
    assert opened == []

    for allowed in ("notes.md", "data.json"):
        assert open_source(str(tmp_path), allowed, verified=True, opener=opened.append).ok
    assert len(opened) == 2

    for forbidden in ("exe", "lnk", "url", "app", "command", "desktop", "msi", "scr"):
        assert forbidden not in OPENABLE_EXTENSIONS
