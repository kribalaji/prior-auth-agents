import logging
from pathlib import Path

import pytest
import yaml

from priorauth.rules import PolicyBook

ROOT = Path(__file__).resolve().parents[1]
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("a2a").setLevel(logging.WARNING)


@pytest.fixture(scope="session")
def book() -> PolicyBook:
    return PolicyBook.load(ROOT / "policies/policies.yaml")


def scenarios() -> list[dict]:
    return yaml.safe_load((ROOT / "evals/scenarios.yaml").read_text())
