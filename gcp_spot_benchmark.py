#!/usr/bin/env python3
"""Run a Stockfish target-vs-master benchmark on a GCP spot VM."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_REPO = "https://github.com/official-stockfish/Stockfish.git"
DEFAULT_INSTANCE_PREFIX = "benchmark-avx512icl"
COMPUTE_ENGINE_SERVICE = "6F81-5844-456A"


def run(
    cmd: list[str], check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def gcloud_json(cmd: list[str]) -> list[dict[str, object]]:
    completed = run([*cmd, "--format=json"], capture=True)
    result = json.loads(completed.stdout)
    if not isinstance(result, list):
        raise SystemExit(f"Expected a JSON list from {' '.join(cmd)}")
    return result


def current_compute_skus() -> list[dict[str, object]]:
    token = run(["gcloud", "auth", "print-access-token"], capture=True).stdout.strip()
    skus: list[dict[str, object]] = []
    page_token = ""

    while True:
        query = {"currencyCode": "USD", "pageSize": "5000"}
        if page_token:
            query["pageToken"] = page_token
        url = (
            f"https://cloudbilling.googleapis.com/v1/services/{COMPUTE_ENGINE_SERVICE}/skus?"
            f"{urlencode(query)}"
        )
        request = Request(url, headers={"Authorization": f"Bearer {token}"})
        with urlopen(request, timeout=60) as response:
            page = json.load(response)
        skus.extend(page.get("skus", []))
        page_token = page.get("nextPageToken", "")
        if not page_token:
            return skus


def sku_price(sku: dict[str, object]) -> float:
    pricing_info = sku.get("pricingInfo", [])
    if not isinstance(pricing_info, list) or not pricing_info:
        raise ValueError("SKU has no pricing information")
    expression = pricing_info[-1]["pricingExpression"]
    rate = expression["tieredRates"][0]["unitPrice"]
    return int(rate.get("units", 0)) + int(rate.get("nanos", 0)) / 1_000_000_000


def choose_cheapest_zone(machine_type: str) -> tuple[str, float]:
    custom_match = re.fullmatch(
        r"([a-z][a-z0-9]*)-custom-([0-9]+)-([0-9]+)(?:-ext)?", machine_type
    )
    machine_filter = (
        f"name~^{custom_match.group(1)}-" if custom_match else f"name={machine_type}"
    )
    machine_types = gcloud_json(
        [
            "gcloud",
            "compute",
            "machine-types",
            "list",
            f"--filter={machine_filter}",
        ]
    )
    if not machine_types:
        raise SystemExit(f"Machine type {machine_type!r} is not available in any zone")

    series = machine_type.split("-", 1)[0].upper()
    if custom_match:
        custom_cpus = int(custom_match.group(2))
        custom_memory_mb = int(custom_match.group(3))
        machine_types = [
            {
                "zone": zone,
                "guestCpus": custom_cpus,
                "memoryMb": custom_memory_mb,
            }
            for zone in sorted({str(machine["zone"]) for machine in machine_types})
        ]
    price_prefixes = {
        "CPU": f"Spot Preemptible {series}{' Custom' if custom_match else ''} Instance Core running ",
        "RAM": f"Spot Preemptible {series}{' Custom' if custom_match else ''} Instance Ram running ",
    }
    regional_prices: dict[str, dict[str, float]] = {}
    for sku in current_compute_skus():
        category = sku.get("category", {})
        if not isinstance(category, dict) or category.get("usageType") != "Preemptible":
            continue
        resource_group = category.get("resourceGroup")
        prefix = price_prefixes.get(str(resource_group))
        if not prefix or not str(sku.get("description", "")).startswith(prefix):
            continue
        taxonomy = sku.get("geoTaxonomy", {})
        regions = taxonomy.get("regions", []) if isinstance(taxonomy, dict) else []
        for region in regions:
            regional_prices.setdefault(str(region), {})[str(resource_group)] = sku_price(sku)

    choices: list[tuple[float, str]] = []
    for machine in machine_types:
        zone = str(machine["zone"])
        region = zone.rsplit("-", 1)[0]
        prices = regional_prices.get(region, {})
        if "CPU" not in prices or "RAM" not in prices:
            continue
        hourly_price = (
            int(machine["guestCpus"]) * prices["CPU"]
            + int(machine["memoryMb"]) / 1024 * prices["RAM"]
        )
        choices.append((hourly_price, zone))

    if not choices:
        raise SystemExit(
            f"Could not find regional spot CPU and RAM prices for machine series {series}"
        )
    hourly_price, zone = min(choices)
    return zone, hourly_price


def instance_status(instance: str, zone: str) -> str | None:
    completed = run(
        [
            "gcloud",
            "compute",
            "instances",
            "describe",
            instance,
            f"--zone={zone}",
            "--format=get(status)",
        ],
        check=False,
        capture=True,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def raise_if_instance_gone(instance: str, zone: str) -> None:
    status = instance_status(instance, zone)
    if status is None:
        raise SystemExit(
            f"GCP instance {instance} is no longer visible in {zone}; "
            "the spot VM was likely preempted/deleted."
        )
    if status != "RUNNING":
        raise SystemExit(
            f"GCP instance {instance} is {status}; the spot VM is no longer running."
        )


def gcloud_ssh(instance: str, zone: str, command: str) -> None:
    completed = run(
        [
            "gcloud",
            "compute",
            "ssh",
            instance,
            f"--zone={zone}",
            "--command",
            command,
        ],
        check=False,
    )
    if completed.returncode != 0:
        raise_if_instance_gone(instance, zone)
        raise subprocess.CalledProcessError(completed.returncode, completed.args)


def create_instance_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        "gcloud",
        "compute",
        "instances",
        "create",
        args.instance,
        f"--zone={args.zone}",
        f"--machine-type={args.machine_type}",
        "--provisioning-model=SPOT",
        "--instance-termination-action=DELETE",
        f"--image-family={args.image_family}",
        f"--image-project={args.image_project}",
        "--no-service-account",
        "--no-scopes",
    ]
    if args.min_cpu_platform:
        cmd.append(f"--min-cpu-platform={args.min_cpu_platform}")
    if args.machine_type.startswith("c4-"):
        cmd.append("--turbo-mode=ALL_CORE_MAX")
    return cmd


def create_instance(args: argparse.Namespace) -> None:
    run(create_instance_command(args))


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
    scp_cmd = [
        "gcloud",
        "compute",
        "scp",
        str(local_runner),
        f"{args.instance}:{remote_runner}",
        f"--zone={args.zone}",
    ]
    for attempt in range(1, 4):
        completed = run(scp_cmd, check=False)
        if completed.returncode == 0:
            break

        raise_if_instance_gone(args.instance, args.zone)
        if attempt == 3:
            raise subprocess.CalledProcessError(completed.returncode, completed.args)

        print(f"scp failed; retrying ({attempt + 1}/3) after 10 seconds", flush=True)
        time.sleep(10)

    return remote_runner


def install_dependencies(args: argparse.Namespace) -> None:
    gcloud_ssh(args.instance, args.zone, "sudo apt-get update")
    gcloud_ssh(
        args.instance,
        args.zone,
        "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential ca-certificates git python3 util-linux",
    )


def run_remote_benchmark(args: argparse.Namespace, remote_runner: str) -> None:
    remote_args = [
        "python3",
        remote_runner,
        args.target_ref,
        "--repo",
        args.repo,
        "--source-dir",
        args.remote_source_dir,
        "--runs",
        str(args.runs),
    ]
    if args.test_repo:
        remote_args.extend(["--test-repo", args.test_repo])
    if args.test_pr:
        remote_args.extend(["--test-pr", str(args.test_pr)])
    if args.arch:
        remote_args.extend(["--arch", args.arch])
    if args.speedtest_args:
        remote_args.extend(["--speedtest-args", args.speedtest_args])
    if args.thresholds is not None:
        remote_args.append("--thresholds")
        remote_args.extend(str(threshold) for threshold in args.thresholds)

    gcloud_ssh(args.instance, args.zone, shlex.join(remote_args))


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a GCP spot VM and compare one Stockfish target against official master.",
    )
    parser.add_argument("target_ref", help="Target commit, branch, tag, or ref to compare against official Stockfish master.")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"Default git URL for the test target. The baseline is always {DEFAULT_REPO} master.")
    parser.add_argument("--test-repo", help="Git URL for the test target. Defaults to --repo.")
    parser.add_argument("--test-pr", type=positive_int, help="GitHub PR number to use as the test target. The positional target_ref is ignored when set.")
    parser.add_argument("--arch", help="Optional Stockfish make ARCH value, for example x86-64-avx512.")
    parser.add_argument("--runs", type=positive_int, default=3, help="Speedtest runs per commit. Default: 3.")
    parser.add_argument("--speedtest-args", default="1 16 30", help='Quoted arguments after speedtest. Default: "1 16 30"')
    parser.add_argument(
        "--thresholds",
        nargs="*",
        type=float,
        help="Speedup thresholds (percent) to report probabilities for. Default: 0 0.25 0.5 1.0.",
    )
    parser.add_argument(
        "--instance",
        default=f"{DEFAULT_INSTANCE_PREFIX}-{uuid.uuid4().hex[:8]}",
        help="GCP instance name. Defaults to a unique benchmark-avx512icl-* name.",
    )
    parser.add_argument("--zone", help="GCP zone. By default, use the cheapest region currently available for the machine type.")
    parser.add_argument("--machine-type", default="c4-highcpu-2", help="GCP machine type.")
    parser.add_argument("--min-cpu-platform", help="Minimum CPU platform.")
    parser.add_argument("--image-family", default="debian-13", help="GCP image family.")
    parser.add_argument("--image-project", default="debian-cloud", help="GCP image project.")
    parser.add_argument("--remote-source-dir", default="/tmp/stockfish-src", help="Remote Stockfish cache directory. Separate base/ and test/ checkouts are stored below it.")
    parser.add_argument("--keep-instance", action="store_true", help="Do not delete the VM after the benchmark.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.zone:
        args.zone, hourly_price = choose_cheapest_zone(args.machine_type)
        print(
            f"Selected {args.zone} for {args.machine_type} at approximately "
            f"${hourly_price:.6f}/hour spot price",
            flush=True,
        )

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
