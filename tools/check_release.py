"""核对 Git 跟踪清单或源码 ZIP/sdist/wheel，不读取密钥值。
用法：python tools/check_release.py [archive.zip|archive.tar.gz|package.whl]
"""
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from pathlib import PurePosixPath


def validate_release_paths(names):
    forbidden = {".git", ".venv", "node_modules", "runs", "store", "tmp", "data",
                 "models", "adapters", "python_sandbox", "__pycache__", "build", "dist", ".vite"}
    bad = []
    for name in names:
        path = PurePosixPath(name.replace("\\", "/"))
        if path.is_absolute() or (path.parts and ":" in path.parts[0]) or ".." in path.parts or set(path.parts) & forbidden:
            bad.append(name)
        elif path.name.startswith(".env") and path.name != ".env.example":
            bad.append(name)
        elif path.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".jsonl", ".safetensors", ".pt", ".bin", ".key", ".pem"}:
            bad.append(name)
        elif path.parts and path.parts[0] in {"examples", "orm_presets"} and path.name != "README.md":
            bad.append(name)
    if bad:
        raise ValueError("发布包包含禁止文件：" + ", ".join(bad))
    return len(names)



if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    if len(sys.argv) == 1:
        names = subprocess.check_output(["git", "ls-files"], cwd=root, text=True, encoding="utf-8").splitlines()
    else:
        archive_path = Path(sys.argv[1])
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path) as archive:
                names = [item.filename for item in archive.infolist() if not item.is_dir()]
        else:
            with tarfile.open(archive_path) as archive:
                names = [item.name for item in archive.getmembers() if item.isfile()]
        if archive_path.suffix != ".whl" and names:
            prefix = names[0].split("/")[0] + "/"
            if prefix.startswith("sqlstage") and all(name.startswith(prefix) for name in names):
                names = [name[len(prefix):] for name in names]
    if not names:
        raise ValueError("发布清单为空")
    print(f"release paths: PASS ({validate_release_paths(names)} files)")
