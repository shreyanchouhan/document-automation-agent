"""The review console is how a human actually resolves a document, so it is smoke tested."""

from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP_PATH = str(Path(__file__).resolve().parent.parent / "console" / "app.py")


def test_console_renders_the_queue_and_the_operations_tab(processed_pipeline):
    app = AppTest.from_file(APP_PATH, default_timeout=300).run()
    assert not app.exception
    assert len(app.tabs) == 2
    assert any("review console" in heading.value.lower() for heading in app.title)
    assert app.selectbox, "the queue selector should list the open reviews"
    assert app.metric, "the operations tab should show throughput metrics"
