"""Intake: unreadable input is a run error with a reason, never a crash.

Regression: the UI accepted .png/.jpg, but no image extraction path exists -
from_file returned an empty Document, intake rejected it as "almost no text",
and the extractor still spent a model call on it. Any other unsupported file
raised straight out of the graph.
"""

from __future__ import annotations

import pytest

from app.tools.documents import UnsupportedDocument, from_file


def test_images_are_refused_with_a_reason(tmp_path):
    img = tmp_path / "resume.png"
    img.write_bytes(b"\x89PNG\r\n")
    with pytest.raises(UnsupportedDocument, match="PDF or DOCX"):
        from_file(img)


def test_web_upload_does_not_accept_images():
    from app.web import ALLOWED_SUFFIXES

    assert not {".png", ".jpg", ".jpeg", ".webp", ".heic"} & ALLOWED_SUFFIXES


def test_bad_file_becomes_a_run_error_without_a_model_call(tmp_path, monkeypatch):
    from app.graph import PIPELINE
    from app.state import new_state

    def no_calls(*args, **kwargs):
        raise AssertionError("no model call should be made for unreadable input")

    for module in ("app.nodes.extract", "app.nodes.matching", "app.nodes.generate"):
        monkeypatch.setattr(f"{module}.complete_json", no_calls)

    img = tmp_path / "resume.png"
    img.write_bytes(b"\x89PNG\r\n")
    final = PIPELINE.invoke(new_state(profile_file=str(img), jd_file=str(img), out_dir=tmp_path))

    errors = " ".join(final["errors"])
    assert "Profile: Image files" in errors and "Job description: Image files" in errors
    assert final.get("scorecard") is None
