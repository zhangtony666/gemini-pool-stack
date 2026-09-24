from dataclasses import replace
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from image_verifier.config import Settings
from image_verifier.images import validate_image
from image_verifier.store import Store


def pytest_configure(config):
    # Avoid a machine-wide pytest temp directory with inherited Windows ACLs.
    if config.option.basetemp is None:
        (Path(__file__).resolve().parents[1] / "work" / "tests").mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(
            Path(__file__).resolve().parents[1] / "work" / "tests" / uuid4().hex
        )


def png(color="red"):
    stream = BytesIO()
    Image.new("RGB", (16, 16), color).save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=tmp_path / "test.sqlite3",
        rpm=60000,
        rph=3600000,
        poll_seconds=0.01,
        lease_seconds=3,
    )


@pytest.fixture
def image(settings):
    return validate_image(png(), "red.png", settings)


@pytest.fixture
def store(settings):
    result = Store(settings)
    result.initialize()
    return result


@pytest.fixture
def store_factory(settings):
    def factory(**kwargs):
        result = Store(replace(settings, **kwargs))
        result.initialize()
        return result

    return factory
