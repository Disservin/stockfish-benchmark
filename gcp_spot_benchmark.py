#!/usr/bin/env python3
"""Run the Stockfish two-commit benchmark on a GCP spot VM."""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path


DEFAULT_REPO = "https://github.com/official-stockfish/Stockfish.git"


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=check, text=True)


def gcloud_ssh(instance: str, zone: str, command: str) -> None:
    run(
        [
            "gcloud",
            "compute",
            "ssh",
            instance,
            f"--zone={zone}",
            "--command",
            command,
        ]
    )


def create_instance(args: argparse.Namespace) -> None:
    run(
        [
            "gcloud",
            "compute",
            "instances",
            "create",
            args.instance,
            f"--zone={args.zone}",
            f"--machine-type={args.machine_type}",
            f"--min-cpu-platform={args.min_cpu_platform}",
            "--provisioning-model=SPOT",
            "--instance-termination-action=DELETE",
            f"--image-family={args.image_family}",
            f"--image-project={args.image_project}",
        ]
    )


def delete_instance(args: argparse.Namespace) -> None:
    run(
        [
            "gcloud",
            "compute",
            "instances",
            "delete",
            args.instance,
            f"--zone={args.zone}",
            "--quiet",
        ],
        check=False,
    )


def copy_runner(args: argparse.Namespace) -> str:
    local_runner = Path(__file__).with_name("benchmark_stockfish.py").resolve()
    if not local_runner.exists():
        raise SystemExit(f"Local runner not found: {local_runner}")

    remote_runner = "/tmp/benchmark_stockfish.py"
    run(
        [
            "gcloud",
            "compute",
            "scp",
            str(local_runner),
            f"{args.instance}:{remote_runner}",
            f"--zone={args.zone}",
        ]
    )
    return remote_runner


def install_dependencies(args: argparse.Namespace) -> None:
    gcloud_ssh(args.instance, args.zone, "sudo apt-get update")
    gcloud_ssh(
        args.instance,
        args.zone,
        "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential ca-certificates git python3",
    )


def run_remote_benchmark(args: argparse.Namespace, remote_runner: str) -> None:
    remote_args = [
        "python3",
        remote_runner,
        args.base_commit,
        args.test_commit,
        "--repo",
        args.repo,
        "--source-dir",
        args.remote_source_dir,
        "--runs",
        str(args.runs),
    ]
    if args.base_repo:
        remote_args.extend(["--base-repo", args.base_repo])
    if args.test_repo:
        remote_args.extend(["--test-repo", args.test_repo])
    if args.base_pr:
        remote_args.extend(["--base-pr", str(args.base_pr)])
    if args.test_pr:
        remote_args.extend(["--test-pr", str(args.test_pr)])
    if args.arch:
        remote_args.extend(["--arch", args.arch])
    if args.speedtest_args:
        remote_args.extend(["--speedtest-args", args.speedtest_args])

    gcloud_ssh(args.instance, args.zone, shlex.join(remote_args))


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a GCP spot VM and run benchmark_stockfish.py against two commits.",
    )
    parser.add_argument("base_commit", help="Baseline commit, branch, tag, or ref.")
    parser.add_argument("test_commit", help="Test commit, branch, tag, or ref.")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"Default git URL for both targets. Default: {DEFAULT_REPO}")
    parser.add_argument("--base-repo", help="Git URL for the baseline target. Defaults to --repo.")
    parser.add_argument("--test-repo", help="Git URL for the test target. Defaults to --repo.")
    parser.add_argument("--base-pr", type=positive_int, help="GitHub PR number to use as the baseline target.")
    parser.add_argument("--test-pr", type=positive_int, help="GitHub PR number to use as the test target.")
    parser.add_argument("--arch", help="Optional Stockfish make ARCH value, for example x86-64-avx512.")
    parser.add_argument("--runs", type=positive_int, default=3, help="Speedtest runs per commit. Default: 3.")
    parser.add_argument("--speedtest-args", default="1 16 30", help='Quoted arguments after speedtest. Default: "1 16 30"')
    parser.add_argument("--instance", default="benchmark-avx512icl", help="GCP instance name.")
    parser.add_argument("--zone", default="us-central1-a", help="GCP zone.")
    parser.add_argument("--machine-type", default="n2-custom-2-2048", help="GCP machine type.")
    parser.add_argument("--min-cpu-platform", default="Intel Ice Lake", help="Minimum CPU platform.")
    parser.add_argument("--image-family", default="debian-12", help="GCP image family.")
    parser.add_argument("--image-project", default="debian-cloud", help="GCP image project.")
    parser.add_argument("--remote-source-dir", default="/tmp/stockfish-src", help="Remote Stockfish checkout directory.")
    parser.add_argument("--keep-instance", action="store_true", help="Do not delete the VM after the benchmark.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    created = False
    try:
        create_instance(args)
        created = True
        remote_runner = copy_runner(args)
        install_dependencies(args)
        run_remote_benchmark(args, remote_runner)
        return 0
    finally:
        if created and not args.keep_instance:
            delete_instance(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
