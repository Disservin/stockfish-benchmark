# Stockfish PR Comment Trigger Setup

This benchmark repository supports two trigger modes:

```text
1. Stockfish repository comments with /bench, forwarded here through repository_dispatch.
2. Benchmark repository issue comments with /benchmark https://github.com/OWNER/REPO/pull/NUMBER.
```

Benchmarks always compare the requested PR or target against `master` from `https://github.com/official-stockfish/Stockfish.git`.

## Stockfish Repository Setup

Copy `docs/stockfish-trigger-benchmark.yml` from this repository into the Stockfish repository at:

```text
.github/workflows/trigger-benchmark.yml
```

Add these Stockfish repository variables:

```text
BENCHMARK_ALLOWED_USERS=maxiboiii,other-maintainer
BENCHMARK_OWNER=YOUR_ORG
BENCHMARK_REPO_NAME=stockfish-benchmark
```

`BENCHMARK_ALLOWED_USERS` is enforced in the Stockfish repository before dispatching the benchmark.

Add this Stockfish repository secret:

```text
BENCHMARK_REPO_DISPATCH_TOKEN
```

`BENCHMARK_REPO_DISPATCH_TOKEN` should be a fine-grained PAT from a maintainer or bot account. It needs access to the benchmark repository with:

```text
Contents: read/write
Metadata: read
```

This permission is required because GitHub treats `repository_dispatch` as a contents-write operation.

After that, an allowlisted user can trigger a benchmark by commenting on a Stockfish PR:

```text
/bench
```

The Stockfish-side workflow sends the PR number and head SHA only. It does not choose the baseline; the benchmark code always uses official Stockfish `master` as the baseline.

## Benchmark Repository Setup

The workflow `.github/workflows/stockfish-pr-benchmark.yml` runs in this repository when the Stockfish repository dispatches an event.

The workflow `.github/workflows/comment-benchmark.yml` runs in this repository only when an allowlisted user comments on issue `#1` with:

```text
/benchmark https://github.com/official-stockfish/Stockfish/pull/6994
```

Add this benchmark repository variable too:

```text
BENCHMARK_ALLOWED_USERS=maxiboiii,other-maintainer
```

This must contain the same users allowed in the Stockfish repository. The benchmark repository validates `client_payload.actor` before creating comment tokens, authenticating to GCP, or starting a VM. This protects against direct `repository_dispatch` calls to the benchmark repository.

Add this benchmark repository secret for commenting back to Stockfish:

```text
STOCKFISH_COMMENT_TOKEN
```

`STOCKFISH_COMMENT_TOKEN` should be a fine-grained PAT from a maintainer or bot account. It needs access to the Stockfish repository with:

```text
Issues: read/write
Pull requests: read/write
Metadata: read
```

The same PAT can be used for both secrets only if it has access to both repositories with the permissions above. Keeping two separate bot tokens is cleaner.

For the benchmark-repository `/benchmark ...` workflow, `STOCKFISH_COMMENT_TOKEN` is the token that posts the started/finished comments on the target PR. If you created a GitHub bot account, create this PAT from that bot account so the PR comments appear as the bot user.

## Fine-Grained PAT Setup

Create the tokens from a maintainer or bot account:

```text
GitHub -> Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens -> Generate new token
```

Recommended setup is two separate fine-grained PATs.

Token 1 goes into the Stockfish repository as:

```text
BENCHMARK_REPO_DISPATCH_TOKEN
```

Token 1 repository access:

```text
Only select repositories: YOUR_ORG/stockfish-benchmark
```

Token 1 permissions:

```text
Contents: read/write
Metadata: read
```

Token 2 goes into this benchmark repository as:

```text
STOCKFISH_COMMENT_TOKEN
```

Token 2 repository access:

```text
Only select repositories: official-stockfish/Stockfish
```

Token 2 permissions:

```text
Issues: read/write
Pull requests: read/write
Metadata: read
```

You can use one token instead, but then it needs access to both repositories and all permissions from both token descriptions above. Two tokens are safer because each token can only do one job.

Use a reasonable expiration date and rotate the tokens when they expire. If you use a bot account, add that bot account to the relevant organization/repositories before creating the tokens.

Add these benchmark repository secrets for GCP Workload Identity Federation:

```text
GCP_WORKLOAD_IDENTITY_PROVIDER
GCP_SERVICE_ACCOUNT
```

The GCP service account needs permission to create, inspect, SSH/SCP to, and delete Compute Engine instances. A practical starting point is:

```text
roles/compute.instanceAdmin.v1
roles/iam.serviceAccountUser
```

You can override benchmark defaults with these benchmark repository variables:

```text
STOCKFISH_OWNER=official-stockfish
STOCKFISH_REPO_NAME=Stockfish
BENCH_ARCH=x86-64-avx512
BENCH_RUNS=3
BENCH_SPEEDTEST_ARGS=1 16 30
GCP_ZONE=us-central1-a
GCP_MACHINE_TYPE=n2-custom-2-2048
GCP_MIN_CPU_PLATFORM=Intel Ice Lake
```

## Security Model

The GCP VM compiles and runs PR code, so treat it as untrusted.

Keep this invariant:

```text
No GitHub or GCP secrets are copied to the GCP VM.
```

The GitHub Actions runner owns credentials, creates the VM, copies only benchmark scripts to it, collects output through SSH, and deletes the VM afterward.
