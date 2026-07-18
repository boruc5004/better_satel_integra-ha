"""Test bootstrap.

Local test env has no Home Assistant install, so register the component
package with its real __path__ but WITHOUT executing its __init__.py (which
imports homeassistant). Modules like `mapping`, `const` and the `pysatel`
protocol library are HA-free and import normally through this stub.
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).parent.parent
COMPONENT = ROOT / "custom_components" / "satel_integra_plus"

_cc = types.ModuleType("custom_components")
_cc.__path__ = [str(ROOT / "custom_components")]
sys.modules.setdefault("custom_components", _cc)

_pkg = types.ModuleType("custom_components.satel_integra_plus")
_pkg.__path__ = [str(COMPONENT)]
sys.modules.setdefault("custom_components.satel_integra_plus", _pkg)
