#!/usr/bin/env python3
"""Build Stockfish master and one target ref, then compare speedtest NPS."""

from __future__ import annotations

import argparse
import math
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
class Target:
    label: str
    repo_url: str
    ref: str
    display_ref: str


@dataclass
class Result:
    target: Target
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
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
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


def fetch_ref(source_dir: Path, target: Target) -> str:
    fetch = run(
        ["git", "fetch", "--tags", target.repo_url, target.ref],
        cwd=source_dir,
        check=False,
    )
    if fetch.returncode == 0:
        return "FETCH_HEAD"

    # A raw commit SHA may not be directly fetchable. Fetch branches from that
    # repository so commits reachable from advertised refs can be checked out.
    run(
        [
            "git",
            "fetch",
            "--tags",
            target.repo_url,
            "+refs/heads/*:refs/remotes/benchmark/*",
        ],
        cwd=source_dir,
    )
    return target.ref


def checkout_target(source_dir: Path, target: Target) -> str:
    checkout_ref = fetch_ref(source_dir, target)
    run(["git", "checkout", "--detach", checkout_ref], cwd=source_dir)
    completed = run(["git", "rev-parse", "HEAD"], cwd=source_dir, capture=True)
    return completed.stdout.strip()


def build_stockfish(source_dir: Path, jobs: int, arch: str | None) -> Path:
    src_dir = source_dir / "src"
    if not src_dir.exists():
        raise SystemExit(f"Stockfish src directory not found: {src_dir}")

    run(["make", "clean"], cwd=src_dir)

    cmd = ["make", f"-j{jobs}", "profile-build"]
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


def benchmark_target(args: argparse.Namespace, target: Target) -> Result:
    resolved_commit = checkout_target(args.source_dir, target)
    binary = build_stockfish(args.source_dir, args.jobs, args.arch)

    nps_values = []
    for run_index in range(1, args.runs + 1):
        print(
            f"speedtest run {run_index}/{args.runs} for {target.label} {target.display_ref}",
            flush=True,
        )
        nps_values.append(run_speedtest(binary, args.speedtest_args))

    return Result(target=target, resolved_commit=resolved_commit, nps_values=nps_values)


def print_summary(base: Result, test: Result) -> None:
    diff = test.mean_nps - base.mean_nps
    diff_stdev = math.sqrt(base.stdev_nps**2 + test.stdev_nps**2)
    diff_stderr = math.sqrt(
        base.stdev_nps**2 / len(base.nps_values)
        + test.stdev_nps**2 / len(test.nps_values)
    )
    if diff_stderr == 0:
        probability_speedup = 1.0 if diff > 0 else 0.5 if diff == 0 else 0.0
    else:
        probability_speedup = normal_cdf(diff / diff_stderr)
    pct = 0.0 if base.mean_nps == 0 else diff * 100.0 / base.mean_nps

    print("\nSummary")
    print("name  ref           resolved                                  mean nps")
    for name, result in (("base", base), ("test", test)):
        print(
            f"{name:4}  "
            f"{result.target.display_ref[:12]:12}  "
            f"{result.resolved_commit[:40]:40}  "
            f"{result.mean_nps:10.0f} +/- {result.stdev_nps:.0f}"
        )

    print(f"\nDifference: {diff:+.0f} +/- {diff_stdev:.0f} nodes/second ({pct:+.3f}%)")
    print(f"P(speedup > 0): {probability_speedup:.6f}")


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone/fetch Stockfish, build official master and one target, run ./stockfish speedtest, and compare Nodes/second.",
    )
    parser.add_argument(
        "target_ref",
        help="Target commit, branch, tag, or ref to compare against official Stockfish master.",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"Default git URL for the test target. The baseline is always {DEFAULT_REPO} master.",
    )
    parser.add_argument(
        "--test-repo",
        help="Git URL for the test target. Defaults to --repo.",
    )
    parser.add_argument(
        "--test-pr",
        type=positive_int,
        help="GitHub PR number to use as the test target from --test-repo/--repo. The positional target_ref is ignored when set.",
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
        default="1 16 30",
        help='Quoted arguments after speedtest. Default: "1 16 30"',
    )
    return parser.parse_args(argv)


def make_targets(args: argparse.Namespace) -> tuple[Target, Target]:
    test_ref = f"refs/pull/{args.test_pr}/head" if args.test_pr else args.target_ref
    test_display = f"PR#{args.test_pr}" if args.test_pr else args.target_ref
    return (
        Target("base", DEFAULT_REPO, "master", "official/master"),
        Target("test", args.test_repo or args.repo, test_ref, test_display),
    )


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    args.source_dir = args.source_dir.resolve()
    args.speedtest_args = shlex.split(args.speedtest_args)

    base_target, test_target = make_targets(args)
    ensure_stockfish_repo(base_target.repo_url, args.source_dir)
    base = benchmark_target(args, base_target)
    test = benchmark_target(args, test_target)
    print_summary(base, test)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
