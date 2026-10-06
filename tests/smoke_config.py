"""Offline configuration check; no network requests or extraction."""
import argparse
import importlib
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "ingestion_sources.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runtime")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        from ingestion_config import ConfigurationError, startup_check
    except ImportError:
        print("[config][error] Missing dependency. Run: python -m pip install -r requirements.txt")
        return 1
    try:
        startup_check(args.config, args.output_dir)
        for name in ("requests", "dotenv", "tenacity", "contracts", "delta_checker",
                     "extractor", "transformer", "main_orchestrator", "extractors.registry"):
            importlib.import_module(name)
    except ConfigurationError as exc:
        print(f"[config][error] {exc}")
        return 1
    except ImportError:
        print("[config][error] Required ingestion module/dependency missing. Reinstall requirements and check the project folder.")
        return 1
    print("[config] Standalone imports: OK")
    print("[config] Offline diagnostic complete; no HTTP requests made.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
