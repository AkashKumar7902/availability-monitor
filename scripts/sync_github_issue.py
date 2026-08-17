#!/usr/bin/env python3
"""Keep one GitHub issue in sync with the monitor's availability state."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


MARKERS = {
    "1": "<!-- availability-monitor:alert -->",
    "2": "<!-- availability-monitor:alert:2 -->",
}


def issue_title(slot: str) -> str:
    return f"✅ Monitored item {slot} is available"


class GitHubAPIError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"GitHub API returned HTTP {status}: {body[:500]}")
        self.status = status


class GitHubClient:
    def __init__(self, api_url: str, repository: str, token: str) -> None:
        owner, repo = repository.split("/", 1)
        self.repository_path = f"{quote(owner, safe='')}/{quote(repo, safe='')}"
        self.api_url = api_url.rstrip("/")
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "availability-monitor",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.api_url}{path}",
            data=data,
            headers=self.headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read()
                return json.loads(body) if body else None
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise GitHubAPIError(error.code, body) from error

    def matching_open_issues(
        self,
        marker: str,
        title: str,
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        page = 1
        while True:
            issues = self.request(
                "GET",
                f"/repos/{self.repository_path}/issues"
                f"?state=open&per_page=100&page={page}",
            )
            if not isinstance(issues, list):
                raise RuntimeError("GitHub returned an unexpected issues response")
            matches.extend(
                issue
                for issue in issues
                if (
                    "pull_request" not in issue
                    and marker in (issue.get("body") or "")
                    and issue.get("title") == title
                    and isinstance(issue.get("user"), dict)
                    and issue["user"].get("login") == "github-actions[bot]"
                )
            )
            if len(issues) < 100:
                return matches
            page += 1


def append_outputs(path: str | None, *, action: str, issue_url: str) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(f"action={action}\n")
        output.write(f"alert_created={'true' if action == 'created' else 'false'}\n")
        output.write(f"issue_url={issue_url}\n")


def issue_body(slot: str, assignee: str) -> str:
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lead = f"@{assignee} — " if assignee else ""
    return f"""{MARKERS[slot]}

{lead}privately configured item {slot} is **available**.

- Checked: {checked_at}

Open your saved product page to order it. The target is intentionally omitted from
this public issue. This alert closes automatically when the item becomes unavailable.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", choices=sorted(MARKERS), default="1")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    slot = args.slot
    status = os.environ.get("STOCK_STATUS", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    assignee = os.environ.get("ALERT_ASSIGNEE", "").strip()

    if status not in {"available", "unavailable"}:
        raise RuntimeError(f"Refusing to sync an inconclusive stock status: {status!r}")
    if repository.count("/") != 1 or not token:
        raise RuntimeError("GITHUB_REPOSITORY and GITHUB_TOKEN are required")

    client = GitHubClient(api_url, repository, token)
    title = issue_title(slot)
    open_issues = client.matching_open_issues(MARKERS[slot], title)
    action = "none"
    issue_url = open_issues[0].get("html_url", "") if open_issues else ""

    if status == "available" and not open_issues:
        payload: dict[str, Any] = {
            "title": title,
            "body": issue_body(slot, assignee),
        }
        if assignee:
            payload["assignees"] = [assignee]
        try:
            issue = client.request(
                "POST", f"/repos/{client.repository_path}/issues", payload
            )
        except GitHubAPIError as error:
            if error.status != 422 or "assignees" not in payload:
                raise
            print(
                f"Warning: could not assign {assignee!r}; creating the alert unassigned.",
                file=sys.stderr,
            )
            payload.pop("assignees")
            issue = client.request(
                "POST", f"/repos/{client.repository_path}/issues", payload
            )
        action = "created"
        issue_url = issue.get("html_url", "")
    elif status == "unavailable" and open_issues:
        for issue in open_issues:
            client.request(
                "PATCH",
                f"/repos/{client.repository_path}/issues/{issue['number']}",
                {"state": "closed", "state_reason": "completed"},
            )
        action = "closed"

    append_outputs(args.github_output, action=action, issue_url=issue_url)
    print(json.dumps({"action": action, "issue_url": issue_url}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
