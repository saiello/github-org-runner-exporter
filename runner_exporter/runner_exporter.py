import os
import time

from prometheus_client import start_http_server, Counter, Gauge
from time import sleep
from logger import get_logger
from github_api import githubApi


logger = get_logger()


class runnerExports:
    def __init__(self):

        # Define metrics to expose
        self.metric_runner_org_status = Gauge(
            "github_runner_org_status",
            "Runner status",
            ["name", "id", "os", "labels", "status"],
        )
        self.metric_runner_org_label_status = Gauge(
            "github_runner_org_label_status",
            "Runner label status",
            ["name", "id", "os", "label", "status"],
        )

        self.metric_runner_org_busy = Gauge(
            "github_runner_org_busy",
            "Runner busy status",
            ["name", "id", "os", "status", "labels", "busy"],
        )

        self.metric_runner_org_running_job = Gauge(
            "github_runner_org_running_job",
            "Currently running job on self-hosted runner",
            ["runner_name", "runner_id", "repository", "workflow"],
        )

    def export_metrics(self, runner_list: list, job_map: dict = None):
        if job_map is None:
            job_map = {}
        current_runners = []
        current_job_keys = set()

        for runner in runner_list:
            agg_labels = self.aggregate_labels(runner["labels"])
            # Export metrics
            self.export_runner_status(runner, agg_labels)
            self.export_runner_busy(runner, agg_labels)
            # Updated active runners list
            current_runners.append(str(runner["id"]))

            if runner.get("busy"):
                self.export_runner_job(runner, job_map)
                info = job_map.get(runner["id"], {})
                current_job_keys.add(
                    (
                        runner.get("name"),
                        str(runner.get("id")),
                        info.get("repository", "repo-nd"),
                        info.get("workflow", "workflow-nd"),
                    )
                )

        # Clean up removed runners
        self.ghostbuster(current_runners)
        self.ghostbuster_jobs(current_job_keys)

    def ghostbuster(self, current_runners):
        """
        Case some runner is deleted this function will remove from the metrics
        """
        # Remove metric_runner_org_status ghost metrics
        active_metrics = self.metric_runner_org_status._metrics.copy()
        for runner in active_metrics:
            if runner[1] not in current_runners:
                logger.debug(
                    f"Removing {runner[0]} metric_runner_org_status metrics. {str(runner)}"
                )
                self.metric_runner_org_status.remove(*runner)

        # Remove metric_runner_org_label_status ghost metrics
        active_metrics = self.metric_runner_org_label_status._metrics.copy()
        for runner in active_metrics:
            if runner[1] not in current_runners:
                logger.debug(
                    f"Removing {runner[0]} metric_runner_org_label_status metrics. {str(runner)}"
                )
                self.metric_runner_org_label_status.remove(*runner)

        # Remove metric_runner_org_busy ghost metrics
        active_metrics = self.metric_runner_org_busy._metrics.copy()
        for runner in active_metrics:
            if runner[1] not in current_runners:
                logger.debug(
                    f"Removing {runner[0]} metric_runner_org_busy metrics. {str(runner)}"
                )
                self.metric_runner_org_busy.remove(*runner)

    def ghostbuster_jobs(self, current_job_keys: set):
        """Remove job metrics for runners that are no longer busy or have changed jobs."""
        active_metrics = self.metric_runner_org_running_job._metrics.copy()
        for labels_tuple in active_metrics:
            if labels_tuple not in current_job_keys:
                logger.debug(
                    f"Removing {labels_tuple[0]} metric_runner_org_running_job metrics. "
                    f"{str(labels_tuple)}"
                )
                self.metric_runner_org_running_job.remove(*labels_tuple)

    def export_runner_job(self, runner: dict, job_map: dict):
        info = job_map.get(runner["id"], {})
        self.metric_runner_org_running_job.labels(
            runner.get("name"),
            str(runner.get("id")),
            info.get("repository", "repo-nd"),
            info.get("workflow", "workflow-nd"),
        ).set(1)

    def aggregate_labels(self, labels: dict):
        """
        Aggregate the runners labels in string
        """
        agg_labels = []
        for label in labels:
            if label["type"] == "custom":
                agg_labels.append(label["name"])

        agg_labels.sort()

        return ",".join(agg_labels)

    def export_runner_status(self, runner: dict, agg_labels: str):
        online = 1
        offline = 0
        if runner.get("status") != "online":
            online = 0
            offline = 1

        self.metric_runner_org_status.labels(
            runner.get("name"), runner.get("id"), runner.get("os"), agg_labels, "online"
        ).set(online)

        self.metric_runner_org_status.labels(
            runner.get("name"),
            runner.get("id"),
            runner.get("os"),
            agg_labels,
            "offline",
        ).set(offline)

        for label in runner["labels"]:
            self.metric_runner_org_label_status.labels(
                runner.get("name"),
                runner.get("id"),
                runner.get("os"),
                label["name"],
                "online",
            ).set(online)

            self.metric_runner_org_label_status.labels(
                runner.get("name"),
                runner.get("id"),
                runner.get("os"),
                label["name"],
                "offline",
            ).set(offline)

    def export_runner_busy(self, runner: dict, agg_labels: str):
        """
        Export Runner busy status and running status
        """
        busy_values = [True, False]
        status_values = ["online", "offline"]

        for busy_value in busy_values:
            for status_value in status_values:
                metric_value = 0
                if (
                    runner.get("busy") == busy_value
                    and runner.get("status") == status_value
                ):
                    metric_value = 1

                self.metric_runner_org_busy.labels(
                    runner.get("name"),
                    runner.get("id"),
                    runner.get("os"),
                    status_value,
                    agg_labels,
                    str(busy_value).lower(),
                ).set(metric_value)


def main():
    REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", 30))
    JOB_REFRESH_INTERVAL = int(os.getenv("JOB_REFRESH_INTERVAL", 120))
    PRIVATE_GITHUB_TOKEN = os.getenv("PRIVATE_GITHUB_TOKEN")
    GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
    GITHUB_PRIVATE_KEY = os.getenv("GITHUB_PRIVATE_KEY")
    OWNER = os.getenv("OWNER")
    API_URL = os.getenv("API_URL", "https://api.github.com")
    MONITORED_REPOS = [r.strip() for r in os.getenv("MONITORED_REPOS", "").split(",") if r.strip()]

    # Start prometheus metrics
    logger.info("Starting metrics server")
    start_http_server(8000)

    runner_exports = runnerExports()

    github = githubApi(
        OWNER,
        logger,
        github_token=PRIVATE_GITHUB_TOKEN,
        github_app_id=GITHUB_APP_ID,
        private_key=GITHUB_PRIVATE_KEY,
        api_url=API_URL,
        monitored_repos=MONITORED_REPOS,
    )

    job_map = {}
    last_job_refresh = 0.0

    while True:
        runners_list = github.list_runners()
        any_busy = any(r.get("busy") for r in runners_list)

        if any_busy:
            job_map = github.get_runner_jobs_map()
        elif not any_busy:
            job_map = {}
        if runners_list:
            runner_exports.export_metrics(runners_list, job_map)

        sleep(REFRESH_INTERVAL)


if __name__ == "__main__":
    main()
