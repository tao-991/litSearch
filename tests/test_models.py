from __future__ import annotations

import json

from litsearch.models import PaperMeta, paper_from_dict, paper_to_dict


def test_paper_round_trips_through_json_without_raw() -> None:
    paper = PaperMeta(
        source="openalex",
        source_id="W1",
        title="Zoë's agents",
        abstract="text",
        authors=("Zoë Müller", "李雷"),
        year=2025,
        doi="10.1/x",
        arxiv_id="2501.00001",
        openalex_id="W1",
        citation_count=3,
        links={"doi": "https://doi.org/10.1/x"},
        oa_pdf_url="https://example.org/a.pdf",
        work_type="article",
        raw={"huge": "payload"},
    )
    data = paper_to_dict(paper)
    assert "raw" not in data
    assert data["authors"] == ["Zoë Müller", "李雷"]
    restored = paper_from_dict(json.loads(json.dumps(data, ensure_ascii=False)))
    assert restored == paper  # equality ignores raw
    assert restored.authors == ("Zoë Müller", "李雷")
    assert restored.raw == {}
