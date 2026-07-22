#!/usr/bin/env python3
"""Build two Stockfish commits and compare speedtest NPS."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_REPO = "https://github.com/official-stockfish/Stockfish.git"
NPS_RE = re.compile(r"^Nodes/second\s*:\s*([0-9][0-9,]*)\s*$", re.MULTILINE)


@dataclass
class Result:
    commit: str
    resolved_commit: str
    nps_values: list[int]

    @property
    def mean_nps(self) -> float:
        return statistics.fmean(self.nps_values)

    @property
    def stdev_nps(self) -> float:
        if len(self.nps_values) < 2:
            return 0.0
        return statistics.stdev(self.nps_values)


def run(
    cmd: list[str],
    cwd: Path | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def ensure_stockfish_repo(repo_url: str, source_dir: Path) -> None:
    if not source_dir.exists():
        run(["git", "clone", repo_url, str(source_dir)])
        return

    git_dir = source_dir / ".git"
    if not git_dir.exists():
        raise SystemExit(f"{source_dir} exists but is not a git repository")

    run(["git", "fetch", "--tags", "origin"], cwd=source_dir)


def checkout_commit(source_dir: Path, commit: str) -> str:
    run(["git", "checkout", "--detach", commit], cwd=source_dir)
    completed = run(["git", "rev-parse", "HEAD"], cwd=source_dir, capture=True)
    return completed.stdout.strip()


def build_stockfish(source_dir: Path, jobs: int, arch: str | None) -> Path:
    src_dir = source_dir / "src"
    if not src_dir.exists():
        raise SystemExit(f"Stockfish src directory not found: {src_dir}")

    run(["make", "clean"], cwd=src_dir)

    cmd = ["make", f"-j{jobs}", "build"]
    if arch:
        cmd.append(f"ARCH={arch}")
    run(cmd, cwd=src_dir)

    binary = src_dir / "stockfish"
    if not binary.exists():
        raise SystemExit(f"Stockfish binary was not created: {binary}")
    return binary


def parse_nps(output: str) -> int:
    match = NPS_RE.search(output)
    if not match:
        raise RuntimeError(
            f"Could not find Nodes/second in speedtest output:\n{output}"
        )
    return int(match.group(1).replace(",", ""))


def run_speedtest(binary: Path, speedtest_args: list[str]) -> int:
    completed = run(
        [str(binary), "speedtest", *speedtest_args], cwd=binary.parent, capture=True
    )
    print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    return parse_nps(completed.stdout)


def benchmark_commit(args: argparse.Namespace, commit: str) -> Result:
    resolved_commit = checkout_commit(args.source_dir, commit)
    binary = build_stockfish(args.source_dir, args.jobs, args.arch)

    nps_values = []
    for run_index in range(1, args.runs + 1):
        print(f"speedtest run {run_index}/{args.runs} for {commit}", flush=True)
        nps_values.append(run_speedtest(binary, args.speedtest_args))

    return Result(commit=commit, resolved_commit=resolved_commit, nps_values=nps_values)


def print_summary(base: Result, test: Result) -> None:
    diff = test.mean_nps - base.mean_nps
    pct = 0.0 if base.mean_nps == 0 else diff * 100.0 / base.mean_nps

    print("\nSummary")
    print("name  commit        resolved                                  mean nps     stdev")
    for name, result in (("base", base), ("test", test)):
        print(
            f"{name:4}  "
            f"{result.commit[:12]:12}  "
            f"{result.resolved_commit[:40]:40}  "
            f"{result.mean_nps:10.0f}  "
            f"{result.stdev_nps:8.0f}"
        )

    print(f"\nDifference: {diff:+.0f} nodes/second ({pct:+.3f}%)")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone/fetch Stockfish, build two commits, run ./stockfish speedtest, and compare Nodes/second.",
    )
    parser.add_argument(
        "base_commit",
        help="Stockfish commit, tag, or ref to use as the baseline.",
    )
    parser.add_argument(
        "test_commit",
        help="Stockfish commit, tag, or ref to compare against the baseline.",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"Stockfish git URL. Default: {DEFAULT_REPO}",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path(".stockfish-src"),
        help="Local Stockfish checkout directory.",
    )
    parser.add_argument(
        "--arch", help="Optional Stockfish make ARCH value, for example x86-64-bmi2."
    )
    parser.add_argument(
        "--jobs",
        type=positive_int,
        default=os.cpu_count() or 1,
        help="Parallel make jobs. Default: CPU count.",
    )
    parser.add_argument(
        "--runs",
        type=positive_int,
        default=1,
        help="Speedtest runs per commit. Default: 1.",
    )
    parser.add_argument(
        "--speedtest-args",
        default="1 16 5",
        help='Quoted arguments after speedtest. Default: "1 16 5"',
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    args.source_dir = args.source_dir.resolve()
    args.speedtest_args = shlex.split(args.speedtest_args)

    ensure_stockfish_repo(args.repo, args.source_dir)
    base = benchmark_commit(args, args.base_commit)
    test = benchmark_commit(args, args.test_commit)
    print_summary(base, test)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
