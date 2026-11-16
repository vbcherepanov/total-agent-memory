import sys
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from tam_db.prepare_vector_index import main as run

    run()
