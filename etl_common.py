"""Deterministic serialization and recoverable publication for ingestion only."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parent
STAGING_PATH = ROOT / "staging_idmc_data.json"
READY_PATH = ROOT / "ready_for_vector_db_v2.json"
STATE_PATH = ROOT / "delta_state_v2.json"
REPORT_PATH = ROOT / "extraction_report_v2.json"
TRANSFORM_VERSION = "2.1.0"

def setup_logging(verbose=False):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

def md5_hex(text):
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()

def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default  # malformed existing state must NOT be silently discarded

def atomic_write_json(path, obj, indent=2):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=indent) + "\n"
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

@contextmanager
def ingestion_lock(directory):
    """Kernel lock releases on process death; the harmless lock file remains."""
    path = Path(directory) / ".ingestion.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another ingestion/transform is using this output directory") from exc
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

def recover_publication(directory):
    """Rollback an interrupted multi-file publish before any new ingestion."""
    directory = Path(directory).resolve()
    journal = directory / ".etl-transaction.json"
    if not journal.exists():
        return
    state = read_json(journal)
    backup = directory / state["backup"]
    if backup.parent != directory or not backup.name.startswith(".etl-transaction-"):
        raise ValueError("Unsafe publication journal")
    for item in state["files"]:
        name = item["name"]
        if Path(name).name != name or name.startswith("."):
            raise ValueError("Unsafe publication target in journal")
        target = directory / name
        if item["existed"]:
            saved = backup / name
            fd, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
            os.close(fd)
            try:
                shutil.copyfile(saved, temporary)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        elif target.exists():
            target.unlink()
    journal.unlink()
    shutil.rmtree(backup)

def publish_bundle(directory, payloads):
    """Prepare all outputs; replace delta last. Rollback on errors or next startup."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    recover_publication(directory)
    # Validate serialization before creating any output.
    for name, value in payloads.items():
        if Path(name).name != name or name.startswith("."):
            raise ValueError("Publication requires plain output filenames")
        canonical_json(value)
    backup = Path(tempfile.mkdtemp(dir=directory, prefix=".etl-transaction-"))
    journal = directory / ".etl-transaction.json"
    files = []
    try:
        for name, value in payloads.items():
            target = directory / name
            files.append({"name": name, "existed": target.exists()})
            if target.exists():
                shutil.copyfile(target, backup / name)
            atomic_write_json(backup / (name + ".new"), value)
        atomic_write_json(journal, {"backup": backup.name, "files": files})
        for name in payloads:
            os.replace(backup / (name + ".new"), directory / name)
        journal.unlink()  # commit point; state was the final replacement
    except BaseException:
        if journal.exists():
            recover_publication(directory)
        raise
    finally:
        if not journal.exists() and backup.exists():
            shutil.rmtree(backup)
