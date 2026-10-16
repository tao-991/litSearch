"""Builds small, valid PDFs with real text for tests (no external tool needed)."""

from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]]) -> bytes:
    """A PDF whose ``pages[i]`` is a list of text lines (Helvetica, 12 pt)."""
    objects: list[bytes] = []

    def add(body: str | bytes) -> int:
        objects.append(body if isinstance(body, bytes) else body.encode("latin-1"))
        return len(objects)

    catalog = add("")  # placeholders, replaced below once the ids are known
    pages_id = add("")
    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    for lines in pages:
        content = "BT /F1 12 Tf 72 720 Td 14 TL\n"
        for line in lines:
            content += f"({_escape(line)}) Tj T*\n"
        content += "ET"
        stream = add(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
        page_ids.append(
            add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {stream} 0 R >>"
            )
        )
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    objects[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)
