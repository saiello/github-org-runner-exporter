import requests
import logging
import time
import json
import jwt
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from dateutil import tz
from prometheus_client import Gauge

RATE_LIMIT_FLOOR = 100

class githubApi:
    app_token = None
    app_token_expire_at = None
    # The number of minutes before the token should be renewed
    renew_token_minutes = 5

    def __init__(
        self,
        github_owner: str,
        logger: logging,
        github_token: str = None,
        github_app_id: str = None,
        private_key: str = None,
        api_url: str = "https://api.github.com",
        monitored_repos: list = None,
    ) -> None:

        if github_owner is None or github_owner.strip() == "":
            raise ValueError("Github owner should not be empty")

        self.metric_runner_api_ratelimit = Gauge(
            "github_runner_api_remain_rate_limit",
            "Github REST API remaining requests rate limit (per hour)",
            ["org"],
        )
        self.metric_runner_graphql_ratelimit = Gauge(
            "github_runner_graphql_remain_rate_limit",
            "Github GraphQL API remaining points rate limit (per hour)",
            ["org"],
        )

        self.github_token = github_token
        self.github_app_id = github_app_id
        self.private_key = private_key
        self.github_owner = github_owner
        self.api_url = api_url
        self.logger = logger
        self._remaining_rate_limit = None
        self.monitored_repos = [
            r if "/" in r else f"{github_owner}/{r}" for r in (monitored_repos or [])
        ]

        self.logger.info("GitHub API initialized with owner: %s, app_id: %s, monitored_repos: %s", github_owner, github_app_id, self.monitored_repos)

    def app_jwt_header(self):
        """
        Creates a JSON Web Token (JWT) for authorization to be used with the GitHub API.
        The JWT includes the current time, an expiration time (10 minutes from the current time), and the GitHub app ID.
        The JWT is signed with the private key provided in the class constructor.

        Returns:
            dict: A dictionary containing the JWT header
        """
        time_since_epoch_in_seconds = int(time.time())
        payload = {
            "iat": time_since_epoch_in_seconds,
            "exp": time_since_epoch_in_seconds + (600),
            "iss": self.github_app_id,
        }

        actual_jwt = jwt.encode(payload, self.private_key, algorithm="RS256")

        return {
            "Authorization": "Bearer {}".format(actual_jwt),
            "Accept": "application/vnd.github.machine-man-preview+json",
        }

    def get_app_token(self):
        """
        Retrieves an app token for use with the GitHub API.
        If the app token is still valid (as determined by the `app_token_expire_at` attribute and the `token_renew_minutes` attribute),
        the existing token is returned. Otherwise, a new token is generated and returned.

        Returns:
            str: The app token for use with the GitHub API
        """
        if self.app_token_expire_at:
            expires_at = datetime.datetime.strptime(
                self.app_token_expire_at, "%Y-%m-%dT%H:%M:%SZ"
            )
            expires_at = expires_at.replace(tzinfo=tz.tzutc())
            now = datetime.datetime.now(tz.tzutc())
            if not expires_at - now < datetime.timedelta(
                minutes=self.renew_token_minutes
            ):
                self.logger.info("The current app token still valid.")
                return self.app_token

        jwt_headers = self.app_jwt_header()

        try:
            instalations_response = requests.get(
                f"{self.api_url}/app/installations", headers=jwt_headers
            )
            instalations_response.raise_for_status()
            instalations = instalations_response.json()
        except requests.exceptions.RequestException as e:
            self.logger.error("An error occured while getting app installations: %s", e)
            raise

        self.logger.info(
            "Looking for installation: login=[%s] app_id=[%s]",
            self.github_owner,
            self.github_app_id,
        )
        # use same approach of myoung34/docker-github-actions-runner
        # see: https://github.com/myoung34/docker-github-actions-runner/blob/master/app_token.sh#L80
        installation = next(
            (
                i
                for i in instalations
                if i["account"]["login"] == self.github_owner
                and str(i["app_id"]) == str(self.github_app_id)
            ),
            None,
        )
        if installation is None:
            raise ValueError(
                f"No GitHub App installation found for owner '{self.github_owner}' "
                f"with app_id '{self.github_app_id}'. "
                f"Available: {[(i['account']['login'], i['app_id']) for i in instalations]}"
            )
        self.logger.info("Found installation %s for '%s'", installation["id"], self.github_owner)

        try:
            resp = requests.post(installation["access_tokens_url"], headers=jwt_headers)
            resp.raise_for_status()
            token_data = json.loads(resp.content)
        except requests.exceptions.RequestException as e:
            self.logger.error("An error occured while getting app token: %s", e)
            raise

        self.app_token_expire_at = token_data["expires_at"]
        self.app_token = token_data["token"]
        self.logger.info(
            f"A new app token has been generated. It will expire on {self.app_token_expire_at}"
        )

        return token_data["token"]

    def get_headers(self):
        """
        Retrieves the headers for use with the GitHub API.
        If a GitHub token is provided, it is used as the "Authorization" header.
        If GitHub app ID is provided, an app token is generated and used as the "Authorization" header.

        Returns:
            dict: A dictionary containing the headers for use with the GitHub API

        Raises:
            ValueError: If neither a GitHub token nor a GitHub app ID is provided
        """
        headers = {}

        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"
        elif self.github_app_id:
            headers["Authorization"] = f"token {self.get_app_token()}"
        else:
            raise ValueError(
                "No token or app ID provided, a Github token or GitHub app is required."
            )

        return headers

    def list_runners(self) -> list:
        """
        Retrieves a list of the registered organization GitHub runners

        Returns:
            list: A list containing the current registered runners
        """
        runners_list = []

        headers = self.get_headers()

        per_page = 100
        url = f"{self.api_url}/orgs/{self.github_owner}/actions/runners?per_page={per_page}"

        while True:
            try:
                self.logger.info(f"Sending the api request for {url}")
                result = requests.get(url, headers=headers)

                if result.headers:
                    remaining_requests = result.headers.get("X-RateLimit-Remaining")
                    self.logger.info(f"Remaining requests: {remaining_requests}")
                    self._remaining_rate_limit = int(remaining_requests)
                    self.metric_runner_api_ratelimit.labels(self.github_owner).set(
                        self._remaining_rate_limit
                    )

                if not result.ok:
                    self.logger.error(
                        f"Api request returned error: {result.reason} {result.text}"
                    )
                    return []

                api_result = result.json()
                runners_list += api_result["runners"]

                if "next" in result.links.keys():
                    url = result.links["next"]["url"]
                else:
                    break
            except Exception as error:
                self.logger.error(f"Exception: {error}")
                return []

        return runners_list

    def get_runner_jobs_map(self) -> dict:
        """
        Returns {runner_id (int): {'repository': 'org/repo', 'workflow': 'CI'}}
        for all runners currently executing a job.
        Returns {} if the rate limit is too low or on any error.
        """
        if (
            self._remaining_rate_limit is not None
            and self._remaining_rate_limit < RATE_LIMIT_FLOOR
        ):
            self.logger.warning(
                f"Rate limit too low ({self._remaining_rate_limit}), skipping job enrichment"
            )
            return {}

        if not self.monitored_repos:
            self.logger.info("No monitored repos configured, skipping job enrichment")
            return {}

        self.logger.info(f"Fetching in-progress runs for {len(self.monitored_repos)} repos")

        # Step 1: single GraphQL query to get all in-progress runs across all monitored repos
        repo_runs = self._graphql_in_progress_runs(self.monitored_repos)

        # Step 2: fetch jobs for all in-progress runs in parallel
        run_triples = [
            (repo, run["id"], run.get("name", "unknown"))
            for repo, runs in repo_runs.items()
            for run in runs
        ]

        if not run_triples:
            return {}

        result = {}
        workers = min(len(run_triples), 10)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_meta = {
                executor.submit(self._list_run_jobs, repo, run_id): (repo, workflow)
                for repo, run_id, workflow in run_triples
            }
            for future in as_completed(future_to_meta):
                repo, workflow = future_to_meta[future]
                for job in future.result():
                    if job.get("status") == "in_progress" and job.get("runner_id"):
                        result[job["runner_id"]] = {
                            "repository": repo,
                            "workflow": workflow,
                        }
        return result

    def _graphql_in_progress_runs(self, repos: list) -> dict:
        """
        Returns {repo_full_name: [run_dict, ...]} for repos that have in-progress runs.
        Uses a single GraphQL request instead of one REST call per repo.
        """
        # Build aliased fragments for each repo so all fit in one request.
        # GraphQL field names can't contain slashes or hyphens, so we use positional aliases.
        fragments = []
        for idx, full_name in enumerate(repos):
            owner, name = full_name.split("/", 1)
            fragments.append(
                f"""
                repo_{idx}: repository(owner: "{owner}", name: "{name}") {{
                    workflowRuns(first: 20) {{
                        nodes {{
                            databaseId
                            name
                            status
                        }}
                    }}
                }}"""
            )

        query = "{ rateLimit { remaining } " + "\n".join(fragments) + " }"
        graphql_url = "https://api.github.com/graphql"
        headers = {**self.get_headers(), "Content-Type": "application/json"}

        try:
            self.logger.info(f"_graphql_in_progress_runs: querying {len(repos)} repos in 1 call")
            resp = requests.post(graphql_url, json={"query": query}, headers=headers)
            if not resp.ok:
                self.logger.error(
                    f"_graphql_in_progress_runs error: {resp.status_code} {resp.reason} {resp.text}"
                )
                return {}
            data = resp.json()
            if "errors" in data:
                self.logger.error(f"_graphql_in_progress_runs GraphQL errors: {data['errors']}")
                return {}
        except Exception as e:
            self.logger.error(f"_graphql_in_progress_runs exception: {e}")
            return {}

        remaining = data.get("data", {}).get("rateLimit", {}).get("remaining")
        if remaining is not None:
            self.logger.debug(f"GraphQL rate limit remaining: {remaining}")
            self.metric_runner_graphql_ratelimit.labels(self.github_owner).set(remaining)

        result = {}
        for idx, full_name in enumerate(repos):
            repo_data = data.get("data", {}).get(f"repo_{idx}")
            if not repo_data:
                continue
            nodes = repo_data.get("workflowRuns", {}).get("nodes", [])
            in_progress = [n for n in nodes if n.get("status") == "IN_PROGRESS"]
            if in_progress:
                self.logger.info(
                    f"_graphql_in_progress_runs: {len(in_progress)} in-progress run(s) in {full_name}"
                )
                result[full_name] = [
                    {"id": n["databaseId"], "name": n["name"]} for n in in_progress
                ]
        return result

    def _list_run_jobs(self, repo_full_name: str, run_id: int) -> list:
        """Returns list of job dicts for the given workflow run."""
        headers = self.get_headers()
        url = f"{self.api_url}/repos/{repo_full_name}/actions/runs/{run_id}/jobs?per_page=100"
        try:
            self.logger.info(f"_list_run_jobs: GET {url}")
            result = requests.get(url, headers=headers)
            self.logger.info(
                f"_list_run_jobs: {result.status_code} {result.reason} [run {run_id}]"
            )
            if not result.ok:
                self.logger.error(
                    f"_list_run_jobs error for run {run_id}: "
                    f"{result.status_code} {result.reason} {result.text}"
                )
                return []
            jobs = result.json().get("jobs", [])
            self.logger.info(f"_list_run_jobs: {len(jobs)} job(s) for run {run_id}")
            return jobs
        except Exception as e:
            self.logger.error(f"_list_run_jobs exception for run {run_id}: {e}")
            return []
