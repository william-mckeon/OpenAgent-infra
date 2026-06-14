"""
tests/conftest.py

Test setup for the openagent-infra proxy (src/api/main.py).

The module under test reads its configuration from environment variables AT
IMPORT TIME (API_KEY, BASE_MODEL_URL, ... see src/api/main.py). It also calls
load_dotenv() at import. load_dotenv() does NOT override variables already
present in os.environ, so by setting our dummy test values here BEFORE the
module is ever imported we guarantee deterministic config regardless of any
.env file sitting in the repo.

We also put the repo root on sys.path so `import src.api.main` resolves.
"""

import os
import sys

# --- Fix sys.path so the `src` package is importable from the repo root. ------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# --- Dummy test configuration, set BEFORE importing the module under test. ----
# These shadow anything in .env (load_dotenv won't override existing values).
TEST_API_KEY = "test-secret-key-for-pytest-1234567890"
TEST_BASE_URL = "http://localhost:9/base"            # never actually reached
TEST_NERVOUS_URL = "http://localhost:9/nervous"      # reachability not exercised

os.environ["API_KEY"] = TEST_API_KEY
os.environ["BASE_MODEL_URL"] = TEST_BASE_URL
os.environ["NERVOUS_SYSTEM_URL"] = TEST_NERVOUS_URL
# Set EMBEDDING_MODEL_URL to empty (NOT just unset) so the "not configured"
# path is exercised. We must set it explicitly to "" rather than pop it:
# load_dotenv() runs at the module's import and would otherwise fill an *absent*
# variable from the repo's .env. An explicitly-set value (even "") is never
# overridden by load_dotenv(), which guarantees a deterministic test config.
os.environ["EMBEDDING_MODEL_URL"] = ""
os.environ["PROVIDER_API_KEY"] = "test-provider-key"
os.environ["REASONING_EFFORT"] = "medium"
# Same reasoning for per-route model names: set to "" so a real .env can't leak
# them in and change which "model" field rides along in the forwarded payload.
os.environ["BASE_MODEL_NAME"] = ""
os.environ["NERVOUS_SYSTEM_MODEL_NAME"] = ""
os.environ["EMBEDDING_MODEL_NAME"] = ""

import pytest  # noqa: E402

# Import the module under test now that the environment is prepared.
from src.api import main as main_module  # noqa: E402


@pytest.fixture(scope="session")
def main():
    """The imported src.api.main module under test."""
    return main_module


@pytest.fixture(scope="session")
def valid_api_key():
    return TEST_API_KEY


@pytest.fixture
def client(main):
    """
    TestClient bound to the FastAPI app.

    Using it as a context manager runs the app lifespan (startup/shutdown),
    which is where REASONING_EFFORT and URL validation happen.
    """
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c
