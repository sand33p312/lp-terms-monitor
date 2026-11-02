"""Local document text extraction helpers."""
from __future__ import annotations
from pathlib import Path


def extract_text(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        return content.decode("utf-8", errors="replace")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(content))
            pages = []
            for page_number, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                pages.append(f"\n[PAGE {page_number}]\n{text}")
            return "\n".join(pages).strip()
        except Exception as exc:
            raise ValueError(f"Could not read PDF: {exc}") from exc
    raise ValueError("Supported document types: PDF, TXT and Markdown")
