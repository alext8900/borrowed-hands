import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "protocol"))


@pytest.fixture(scope="session")
def vectors():
    return json.loads((ROOT / "protocol" / "test-vectors" / "vectors.json").read_text(encoding="utf-8"))
