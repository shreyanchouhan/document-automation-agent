import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.generate_corpus import generate  # noqa: E402
from docagent.config import load_config  # noqa: E402
from docagent.pipeline import Pipeline  # noqa: E402


@pytest.fixture(scope="session")
def corpus_directory(tmp_path_factory) -> Path:
    """A small corpus of its own, so tests never touch the checked-out data."""
    directory = tmp_path_factory.mktemp("corpus")
    generate(directory, invoice_count=80)
    return directory


@pytest.fixture(scope="session")
def config(corpus_directory, tmp_path_factory):
    state = tmp_path_factory.mktemp("state")
    previous = {
        key: os.environ.get(key)
        for key in ("DOCAGENT_CORPUS_DIR", "DOCAGENT_DB_PATH", "DOCAGENT_BASELINE_PATH", "DOCAGENT_BACKEND")
    }
    os.environ["DOCAGENT_CORPUS_DIR"] = str(corpus_directory)
    os.environ["DOCAGENT_DB_PATH"] = str(state / "docagent.db")
    os.environ["DOCAGENT_BASELINE_PATH"] = str(state / "baseline.json")
    os.environ["DOCAGENT_BACKEND"] = "offline"
    yield load_config()
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(scope="session")
def processed_pipeline(config) -> Pipeline:
    pipeline = Pipeline(config)
    pipeline.process_all()
    return pipeline


@pytest.fixture
def pipeline(config) -> Pipeline:
    return Pipeline(config)
