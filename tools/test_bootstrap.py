"""隔离每个原生测试脚本的默认工作台 Store。"""
import runpy
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlstage.store import Store


def run_isolated(script, root, store_class):
    original = store_class.__init__.__defaults__
    with TemporaryDirectory(prefix="sqlstage-tests-") as directory:
        store_class.__init__.__defaults__ = (Path(directory) / "studio.db",)
        sys.path.insert(0, str(root))
        try:
            runpy.run_path(str(script), run_name="__main__")
        finally:
            store_class.__init__.__defaults__ = original
            sys.path.pop(0)


if __name__ == "__main__":
    run_isolated(Path(sys.argv[1]), ROOT, Store)
