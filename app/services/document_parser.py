"""
Document Parser - Phase 3 (Structure Scanner)
converts a documents raw bytes into clean markdown for scoring/reformation.
- PDF,DOCX -> Docling
TXT,MD -> direct decode (no parsing needed)
"""
import os
from io import BytesIO

SUPPORTED_MIME_TYPES ={
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "text/plain",
    "text/markdown",
}

class UnsupportedDocumentTypeError(Exception):
    pass

class DocumentParseError(Exception):
    pass

_converter = None


def _get_converter():
    """
    Lazily builds and caches ONE DocumentConverter for the process.
    Constructing it loads the table-structure model from disk — doing that
    on every upload (as a per-call converter would) was the dominant cost by
    far, dwarfing the actual page parsing. Safe to share across calls:
    DocumentConverter.convert() takes the input per call and holds no
    per-document state on the instance.
    """
    global _converter
    if _converter is None:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            AcceleratorDevice,
            AcceleratorOptions,
            PdfPipelineOptions,
            TableFormerMode,
            TableStructureOptions,
        )
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend

        # Tuned for speed: no OCR (the biggest single cost — skips loading
        # and running an OCR model on every page) and no page-image
        # rendering, since we only need text/table structure right now, not
        # scanned-image or figure handling. pypdfium2 is also a lighter/
        # faster PDF backend than Docling's default parser for that scope.
        # Table structure recognition stays on (FAST mode) so tables still
        # come out usable — it's a small model and cheap relative to OCR.
        # num_threads uses all available cores since this runs synchronously
        # per upload; drop this if parsing ever moves onto a shared/limited
        # worker where hogging every core would starve other requests.
        pdf_pipeline_options = PdfPipelineOptions(
            do_ocr=False,
            generate_page_images=False,
            do_table_structure=True,
            table_structure_options=TableStructureOptions(mode=TableFormerMode.FAST),
            accelerator_options=AcceleratorOptions(
                num_threads=os.cpu_count() or 4,
                device=AcceleratorDevice.AUTO,
            ),
        )
        _converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=pdf_pipeline_options,
                    backend=PyPdfiumDocumentBackend,
                ),
            },
        )
    return _converter


# A minimal, hand-built one-page PDF (no external dependency — reportlab is
# test-only per requirements.txt) used solely to force Docling's models to
# load. Docling loads its layout/table-structure model weights lazily on the
# FIRST converter.convert() call, not when DocumentConverter() is
# constructed — building the converter alone is near-instant; the ~20-30s
# cost is entirely inside that first real conversion. Without warm_up(),
# whichever request happens to be the first PDF/DOCX upload after process
# start eats that cost live — bad for a demo. See warm_up() below.
_WARMUP_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
    b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"5 0 obj<</Length 44>>stream\nBT /F1 12 Tf 20 100 Td (Warmup) Tj ET\nendstream endobj\n"
    b"xref\n0 6\n0000000000 65535 f \n"
    b"trailer<</Size 6/Root 1 0 R>>\n"
    b"startxref\n0\n%%EOF"
)


def warm_up() -> None:
    """
    Forces Docling's model weights to load now, synchronously, instead of on
    whichever request happens to hit this module first. Call this once at
    process startup (see app/main.py) — ideally well before a demo, since
    it's a real ~20-30s blocking cost the first time it runs in a process.
    Safe to call more than once; only the first call actually does anything.
    """
    parse_document_to_markdown(_WARMUP_PDF, "application/pdf", "warmup.pdf")


def parse_document_to_markdown(file_data: bytes, mime_type:str, filename:str) -> str:
    """
    Parse a document's raw bytes into markdown.
    Args:
        file_data: raw bytes (as stored in document_versions.file_data)
        mime_type: the document's mime_type (already validated at upload time, Phase 1 §13.4)
        filename: original filename — used by Docling to infer format from extension

    Returns:
        Markdown string.

    Raises:
        UnsupportedDocumentTypeError: mime_type not in SUPPORTED_MIME_TYPES
        DocumentParseError: parsing failed for a supported type
    """

    if mime_type not in SUPPORTED_MIME_TYPES:
        raise UnsupportedDocumentTypeError(f"Unsupported mime_type: {mime_type}")
    if mime_type in {"text/plain", "text/markdown"}:
        try:
            return file_data.decode("utf-8")  #bytes -> str
        except UnicodeDecodeError:
            return file_data.decode("utf-8", errors="replace")

    try:
        from docling.datamodel.base_models import DocumentStream

        converter = _get_converter()
        stream = DocumentStream(name=filename, stream=BytesIO(file_data))
        result = converter.convert(stream)
        return result.document.export_to_markdown()
    except Exception as e:
        raise DocumentParseError(f"Failed to parse document '{filename}': {e}") from e