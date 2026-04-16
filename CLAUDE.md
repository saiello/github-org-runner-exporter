# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

A Prometheus metrics exporter for GitHub Actions self-hosted runners. It polls the GitHub API on a configurable interval and exposes runner status/busy metrics on port `8000` for scraping.

## Running Locally

Install dependencies:
```bash
pip install -r requirements.txt
```

Run the exporter (from repo root):
```bash
cd runner_exporter
OWNER=myorg GITHUB_APP_ID=123 GITHUB_PRIVATE_KEY="..." python runner_exporter.py
# or with a personal token:
OWNER=myorg PRIVATE_GITHUB_TOKEN=ghp_xxx python runner_exporter.py
```

Run with Docker:
```bash
docker build -t runner-exporter .
docker run -e OWNER=myorg -e PRIVATE_GITHUB_TOKEN=ghp_xxx runner-exporter
```

## Linting & Formatting

```bash
# Format with black (max line length 100 per .flake8)
black runner_exporter/

# Lint with flake8
flake8 runner_exporter/

# Run all pre-commit hooks
pre-commit run --all-files
```

There are no automated tests in this project.

## Code Architecture

The application has three modules under `runner_exporter/`, which is also the Docker `WORKDIR` — imports use bare module names (e.g. `from github_api import githubApi`), not package-qualified names.

- **`runner_exporter.py`** — entry point and main loop. Instantiates `runnerExports` (holds all `prometheus_client` Gauge objects) and `githubApi`, then calls `github.list_runners()` → `runner_exports.export_metrics()` every `REFRESH_INTERVAL` seconds.

- **`github_api.py`** — wraps the GitHub REST API. Supports two auth modes: personal token (`PRIVATE_GITHUB_TOKEN`) or GitHub App (`GITHUB_APP_ID` + `GITHUB_PRIVATE_KEY`). For App auth it generates a JWT, fetches an installation access token, caches it, and renews it 5 minutes before expiry. Handles pagination via `Link` headers. Also exposes a `github_runner_api_remain_rate_limit` Gauge.

- **`logger.py`** — thin wrapper; returns the stdlib `logging` module configured from `LOG_LEVEL` env var.

### Metrics Exposed

| Metric | Labels | Description |
|--------|--------|-------------|
| `github_runner_org_status` | name, id, os, labels, status | 1/0 per runner per online/offline state |
| `github_runner_org_label_status` | name, id, os, label, status | Same but one series per individual label |
| `github_runner_org_busy` | name, id, os, status, labels, busy | 1/0 per runner per busy×status combination |
| `github_runner_api_remain_rate_limit` | org | Remaining GitHub API rate limit |

The `ghostbuster()` method in `runnerExports` removes stale Prometheus label sets when runners are deregistered.

Custom runner labels are aggregated into a sorted comma-separated string (e.g. `"docker,scale"`); `self-hosted` and OS labels (type `"system"`) are excluded from the aggregate but included in `github_runner_org_label_status`.

## Deployment

Helm chart is at `deploy/helm-chart/prometheus-org-runner-exporter/`. Config is passed via `env` or `envFromSecret` in `values.yaml`. The chart includes a `ServiceMonitor` (for Prometheus Operator) and `PrometheusRules` templates, both enabled by default.

Metrics can drive KEDA autoscaling — see README for a `ScaledObject` example using `github_runner_org_busy`.
