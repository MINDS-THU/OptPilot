import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent

RUNTIME_FILES = [
    "oracle_runner.py",
    "build_test_suite.py",
    "build_description.py",
    "checker.py",
    "checker_utils.py",
    "kpi_utils.py",
    "master_pipeline.py",
]

OPTIONAL_DOC_FILES = [
    "oracle_runner.md",
    "build_test_suite.md",
    "checker.md",
    "scenario_blueprint.md",
    "track_design.md",
]


def find_blueprint_file(blueprint_dir: Path, explicit_file: str | None) -> Path:
    if explicit_file:
        candidate = Path(explicit_file)
        if not candidate.is_absolute():
            candidate = blueprint_dir / explicit_file
        candidate = candidate.resolve()
        if not candidate.exists():
            raise FileNotFoundError(f"指定 blueprint 不存在: {candidate}")
        if candidate.suffix != ".py":
            raise ValueError(f"blueprint 必须是 .py 文件: {candidate}")
        return candidate

    py_files = sorted(p for p in blueprint_dir.glob("*.py") if p.is_file())
    if len(py_files) != 1:
        raise ValueError(
            f"目录 {blueprint_dir} 中应当且仅应当有 1 个 blueprint .py 文件，当前找到 {len(py_files)} 个。"
        )
    return py_files[0].resolve()


def ensure_output_dir(output_dir: Path, overwrite: bool):
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise FileExistsError(
            f"输出目录非空: {output_dir}。如需覆盖，请加 --overwrite。"
        )

    output_dir.mkdir(parents=True, exist_ok=True)


def copy_runtime_files(output_dir: Path):
    for name in RUNTIME_FILES:
        src = SCRIPT_DIR / name
        if not src.exists():
            raise FileNotFoundError(f"缺少运行时文件: {src}")
        shutil.copy2(src, output_dir / name)

    for name in OPTIONAL_DOC_FILES:
        src = SCRIPT_DIR / name
        if src.exists():
            shutil.copy2(src, output_dir / name)


def copy_blueprint(blueprint_file: Path, output_dir: Path):
    shutil.copy2(blueprint_file, output_dir / "scenario_blueprint.py")


def run_master_pipeline(output_dir: Path, oracle_runs: int | None, use_llm: bool):
    cmd = [sys.executable, "master_pipeline.py", "--blueprint", "scenario_blueprint.py"]
    if oracle_runs is not None:
        cmd.extend(["--oracle-runs", str(oracle_runs)])
    if use_llm:
        cmd.append("--use-llm")

    print(f"[RUN] {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(output_dir), text=True)
    if result.returncode != 0:
        raise RuntimeError("master_pipeline 执行失败。")


def print_artifacts(output_dir: Path):
    print("\n✅ 数据点构建完成。关键产物:")
    paths = [
        output_dir / "scenario_blueprint.py",
        output_dir / "description.yaml",
        output_dir / "config.json",
        output_dir / "outputs",
    ]
    for p in paths:
        status = "存在" if p.exists() else "未生成"
        print(f"- {p}: {status}")


def main():
    parser = argparse.ArgumentParser(
        description="从仅含 blueprint 的目录构建标准完整数据点。"
    )
    parser.add_argument("--blueprint-dir", required=True, help="包含 blueprint 的目录。")
    parser.add_argument(
        "--blueprint-file",
        default=None,
        help="可选，显式指定 blueprint 文件名或路径。未提供时要求目录中仅有 1 个 .py 文件。",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="输出数据点目录。默认: <blueprint-dir>/<blueprint_stem>_datapoint",
    )
    parser.add_argument("--oracle-runs", type=int, default=None, help="可选，覆盖 golden 数据生成轮数。")
    parser.add_argument("--use-llm", action="store_true", help="启用 build_description 的 LLM 润色。")
    parser.add_argument("--prepare-only", action="store_true", help="仅复制和组装文件，不执行 master_pipeline。")
    parser.add_argument("--overwrite", action="store_true", help="允许写入到非空输出目录。")
    args = parser.parse_args()

    blueprint_dir = Path(args.blueprint_dir).resolve()
    if not blueprint_dir.exists() or not blueprint_dir.is_dir():
        raise NotADirectoryError(f"blueprint-dir 非法: {blueprint_dir}")

    blueprint_file = find_blueprint_file(blueprint_dir, args.blueprint_file)

    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
    else:
        output_dir = blueprint_dir / f"{blueprint_file.stem}_datapoint"

    ensure_output_dir(output_dir, args.overwrite)
    copy_runtime_files(output_dir)
    copy_blueprint(blueprint_file, output_dir)

    print(f"[INFO] blueprint: {blueprint_file}")
    print(f"[INFO] datapoint 输出目录: {output_dir}")

    if not args.prepare_only:
        run_master_pipeline(output_dir, args.oracle_runs, args.use_llm)
        print_artifacts(output_dir)
    else:
        print("[INFO] --prepare-only 模式，已完成文件拷贝。")


if __name__ == "__main__":
    main()
