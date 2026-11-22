import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Mock optional heavyweight ML libraries if not present in the local runner environment
for mod_name in ("xgboost", "xgboost.core"):
    if mod_name not in sys.modules:
        try:
            __import__(mod_name)
        except ImportError:
            mod = types.ModuleType(mod_name)
            class MockXGBClassifier:
                pass
            mod.XGBClassifier = MockXGBClassifier
            sys.modules[mod_name] = mod
