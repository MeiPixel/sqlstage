"""代码-only 发布清单的回归测试，完全不读取私有文件。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.check_release import validate_release_paths


if __name__ == "__main__":
    good = ["README.md", "sqlstage/engine.py", "frontend/package-lock.json",
            ".env.example", "examples/README.md", "orm_presets/README.md", "tests/support.py"]
    assert validate_release_paths(good) == 7
    for name in [".git/config", ".venv/pyvenv.cfg", "runs/studio.db", "tmp/a.py",
                 "frontend/node_modules/a/index.js", "data/questions.json", "x.sqlite",
                 "models/a.safetensors", ".env.local", "examples/datasets/private.json",
                 "orm_presets/private.json", "..\\outside.py", "/root/a.py", "keys/p.key",
                 "C:\\private\\a.py", ".vite/cache.json"]:
        try:
            validate_release_paths([name])
        except ValueError:
            pass
        else:
            raise AssertionError(f"未拒绝：{name}")
    assert validate_release_paths([]) == 0
    print("test_release_paths: PASS")
