"""Test-wide setup.

The DATABASE_URL override MUST happen before anything imports
`app.config`, because `settings` is instantiated at that module's import
time. pytest imports conftest.py before any test module, which is what
makes this work -- and why it is here rather than in a fixture.

Without it, the API tests would read and write the developer's real
./kairo.db and their results would depend on whatever the last manual
`/simulate/run` left behind.
"""

import os
import tempfile

_TEST_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="kairo_tests_"), "test.db").replace("\\", "/")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
os.environ.setdefault("WEBHOOK_SHARED_SECRET", "test_secret_for_hmac")
# Explicitly empty: no test may ever reach the real Anthropic API, whatever
# is set in the developer's shell.
os.environ["ANTHROPIC_API_KEY"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def client():
    """A fresh, empty database per test. The app's own lifespan runs (so
    the decision matrix and the Explainer are wired exactly as in
    production); the tables are dropped and recreated around it."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(bind=engine)
