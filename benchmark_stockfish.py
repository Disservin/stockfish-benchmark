#!/usr/bin/env python3
"""Build Stockfish master and one target ref, then compare speedtest NPS."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
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

    @property
    def ci95_nps(self) -> float:
        return ci95_mean(self.nps_values)


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


def source_dir_for_target(cache_dir: Path, target: Target) -> Path:
    return cache_dir / target.label


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


def speedtest_command(binary: Path, speedtest_args: list[str]) -> list[str]:
    cmd = [str(binary), "speedtest", *speedtest_args]
    if platform.system() == "Linux":
        return ["taskset", "-c", "0", *cmd]
    return cmd


def run_speedtest(binary: Path, speedtest_args: list[str]) -> int:
    completed = run(
        speedtest_command(binary, speedtest_args),
        cwd=binary.parent,
        capture=True,
    )
    print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    return parse_nps(completed.stdout)


def benchmark_target(args: argparse.Namespace, target: Target) -> Result:
    source_dir = source_dir_for_target(args.source_dir, target)
    args.source_dir.mkdir(parents=True, exist_ok=True)
    ensure_stockfish_repo(target.repo_url, source_dir)
    resolved_commit = checkout_target(source_dir, target)
    binary = build_stockfish(source_dir, args.jobs, args.arch)

    nps_values = []
    for run_index in range(1, args.runs + 1):
        print(
            f"speedtest run {run_index}/{args.runs} for {target.label} {target.display_ref}",
            flush=True,
        )
        nps_values.append(run_speedtest(binary, args.speedtest_args))

    return Result(target=target, resolved_commit=resolved_commit, nps_values=nps_values)


def benchmark_report(base: Result, test: Result) -> dict[str, object]:
    diff = test.mean_nps - base.mean_nps
    diff_stderr = mean_stderr(base.nps_values, test.nps_values)
    diff_ci95 = 1.96 * diff_stderr

    if diff_stderr == 0:
        probability_speedup = 1.0 if diff > 0 else 0.5 if diff == 0 else 0.0
    else:
        probability_speedup = normal_cdf(diff / diff_stderr)

    pct = 0.0 if base.mean_nps == 0 else diff * 100.0 / base.mean_nps
    speedup_ci95 = 0.0 if base.mean_nps == 0 else 100.0 * diff_ci95 / base.mean_nps

    return {
        "base": result_report(base),
        "test": result_report(test),
        "diff_nps": diff,
        "diff_ci95_nps": diff_ci95,
        "speedup_percent": pct,
        "speedup_ci95_percent": speedup_ci95,
        "probability_speedup": probability_speedup,
    }


def mean_stderr(base_values: list[int], test_values: list[int]) -> float:
    base_variance = statistics.variance(base_values) if len(base_values) >= 2 else 0.0
    test_variance = statistics.variance(test_values) if len(test_values) >= 2 else 0.0
    return math.sqrt(base_variance / len(base_values) + test_variance / len(test_values))


def result_report(result: Result) -> dict[str, object]:
    return {
        "ref": result.target.display_ref,
        "resolved": result.resolved_commit,
        "runs": len(result.nps_values),
        "mean_nps": result.mean_nps,
        "ci95_nps": result.ci95_nps,
    }


def print_summary(base: Result, test: Result) -> None:
    report = benchmark_report(base, test)

    print("\nSummary")
    print("name  ref           resolved                                  runs          mean nps        95% CI")
    for name, result in (("base", base), ("test", test)):
        print(
            f"{name:4}  "
            f"{result.target.display_ref[:12]:12}  "
            f"{result.resolved_commit[:40]:40}  "
            f"{len(result.nps_values):4d}  "
            f"{result.mean_nps:16.0f}  +/- {result.ci95_nps:8.0f}"
        )

    print(f"\nDifference: {report['diff_nps']:+.0f} +/- {report['diff_ci95_nps']:.0f} nodes/second")
    print(f"Speedup: {report['speedup_percent']:+.5f}% +/- {report['speedup_ci95_percent']:.3f}%")
    print(f"P(speedup > 0): {report['probability_speedup']:.6f}")
    print(f"BENCHMARK_RESULT_JSON {json.dumps(report, sort_keys=True)}")


def ci95_mean(values: list[int]) -> float:
    if len(values) < 2:
        return 0.0
    return 1.96 * statistics.stdev(values) / math.sqrt(len(values))


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
        help="Local Stockfish cache directory. Separate base/ and test/ checkouts are stored below it.",
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
    base = benchmark_target(args, base_target)
    test = benchmark_target(args, test_target)
    print_summary(base, test)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
