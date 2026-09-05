"""Offline import/configuration smoke check; makes no network requests."""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from Module.config import load_gallery_configs  # noqa: E402

if __name__ == "__main__":
    for path in (Path(__file__).resolve().parent.parent / "Module").glob("*.py"):
        importlib.import_module("Module." + path.stem)
    for name in ("web_app", "launcher", "run_gallery", "run_web_gallery", "run_web_server"):
        importlib.import_module(name)
    print(f"Imports OK; {len(load_gallery_configs())} gallery configurations valid")
