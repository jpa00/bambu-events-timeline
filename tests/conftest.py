"""Test setup.

Without Home Assistant installed, only the pure-Python modules (parser,
estimator, file finder) can be tested. Importing them normally runs the package
``__init__.py``, which imports Home Assistant, so bare package modules are
registered first to skip it. Relative imports between modules still work.

With Home Assistant's test harness installed (see README), the integration
tests in ``test_integration.py`` run too.
"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

try:
    import homeassistant  # noqa: F401

    HAS_HOMEASSISTANT = True
except ImportError:
    HAS_HOMEASSISTANT = False
    for name, path in (
        ("custom_components", ROOT / "custom_components"),
        ("custom_components.bambu_timeline", ROOT / "custom_components" / "bambu_timeline"),
    ):
        if name not in sys.modules:
            package = types.ModuleType(name)
            package.__path__ = [str(path)]
            sys.modules[name] = package
