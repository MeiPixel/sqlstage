"""一键跑全部测试脚本。纯原生，不引入任何测试框架。

用法：

    python tools/run_tests.py                # 离线测试；隔离 Store；不连接 MySQL/PG
    python tools/run_tests.py read_only      # 只跑名字含 read_only 的脚本
    python tools/run_tests.py --verbose      # 失败时打印完整输出
    python tools/run_tests.py adapters --integration  # 显式允许重建专用测试库

约定：tests/test_*.py 每个都是可独立执行 entrypoint，用原生 assert 断言，
脚本自己负责把项目根加进 sys.path。本文件只负责遍历、计时与汇总。

需要外部服务的脚本（MySQL / PostgreSQL）自己先探测端口，探测不到就打印一行
ENV_SKIP 再 exit(0)。汇总据此归入「跳过」——这样断言真失败时仍然报 FAIL，
不会被当成环境依赖吞掉。
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# 需要外部服务的脚本自己探测，探测不到就打印 ENV_SKIP 并 exit(0)。
# 判定只看这个标记，不维护名单——否则真 bug 会被"环境依赖"吞掉。
ENV_SKIP = "ENV_SKIP"


def select_scripts(tests_dir, pattern="", integration=False):
    scripts = sorted(Path(tests_dir).glob("test_*.py"))
    return [path for path in scripts if pattern in path.name
            and (integration or path.name != "test_adapters.py")]


def main(argv):
    args = [item for item in argv if not item.startswith("--")]
    verbose = "--verbose" in argv
    pattern = args[0] if args else ""

    scripts = select_scripts(TESTS, pattern, "--integration" in argv)
    if "--integration" in argv:
        print("注意：适配器集成测试会重建 sqlstage_test 测试库。只允许对专用测试服务执行。")
    if not scripts:
        print(f"没有匹配的测试脚本：{TESTS / 'test_*.py'}")
        return 1

    passed, failed, skipped = [], [], []
    started = time.time()
    for index, path in enumerate(scripts, 1):
        label = f"[{index}/{len(scripts)}] {path.name}"
        print(f"{label} ... ", end="", flush=True)
        begun = time.time()
        result = subprocess.run([sys.executable, str(ROOT / "tools/test_bootstrap.py"), str(path)], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace")
        elapsed = time.time() - begun
        tail = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0 and ENV_SKIP in tail:
            # 脚本自己判断缺外部服务，与代码无关
            skipped.append(path.name)
            reason = next((line.strip() for line in tail.splitlines() if ENV_SKIP in line), "")
            print(f"SKIP {reason.replace(ENV_SKIP, '').strip() or '缺少外部服务'}")
            continue
        if result.returncode == 0:
            passed.append(path.name)
            print(f"OK ({elapsed:.1f}s)")
            continue
        failed.append(path.name)
        print(f"FAIL ({elapsed:.1f}s)")
        if verbose:
            print(tail.rstrip())
        else:
            lines = [line for line in tail.strip().splitlines() if line.strip()]
            for line in lines[-3:]:
                print(f"      {line}")

    total = time.time() - started
    print()
    print(f"通过 {len(passed)} · 失败 {len(failed)} · 跳过 {len(skipped)} · 用时 {total:.1f}s")
    for name in failed:
        print(f"  [FAIL] {name}")
    for name in skipped:
        print(f"  [SKIP] {name}（缺少外部服务，非代码缺陷）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
