"""Enforce the standalone ingestion boundary without external services."""
import ast
import importlib
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class StandaloneTests(unittest.TestCase):
    def test_all_application_imports_resolve_locally(self):
        modules = ["contracts", "extractor", "delta_checker", "etl_common", "transformer",
                   "main_orchestrator", "dwh_client", "dashboards",
                   "extractors.base", "extractors.superset", "extractors.cctv",
                   "extractors.tableau_looker", "extractors.registry",
                   "transformers.staging", "transformers.chunking"]
        for name in modules:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                self.assertTrue(Path(module.__file__).resolve().is_relative_to(ROOT))

    def test_no_downstream_or_legacy_service_imports(self):
        excluded = {"fastapi", "google", "chromadb", "vector_store", "app", "chart_service", "dwh_ingest"}
        paths = [*ROOT.glob("*.py"), *(ROOT / "extractors").glob("*.py"),
                 *(ROOT / "transformers").glob("*.py")]
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                imports = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                           else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                self.assertFalse(excluded.intersection(name.split(".")[0] for name in imports), path.name)

    def test_orchestrator_stops_at_output(self):
        import inspect
        from main_orchestrator import run
        parameters = inspect.signature(run).parameters
        self.assertNotIn("load_vector_db", parameters)
        self.assertNotIn("refresh_metadata", parameters)
