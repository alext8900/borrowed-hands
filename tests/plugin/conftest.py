"""Load the plugin directory `borrowed-hands/` as the package `borrowed_hands` (the dir name has a dash)."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "borrowed-hands"
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "protocol"))


def load_plugin_package():
    if "borrowed_hands" in sys.modules:
        return sys.modules["borrowed_hands"]
    spec = importlib.util.spec_from_file_location(
        "borrowed_hands", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["borrowed_hands"] = mod
    spec.loader.exec_module(mod)
    return mod


load_plugin_package()


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "plugin-data"
    d.mkdir(mode=0o700)
    return d
