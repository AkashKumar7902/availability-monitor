from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import sync_github_issue


class FakeGitHubClient:
    repository_path = "owner/repository"

    def __init__(self, open_issues: list[dict]) -> None:
        self.open_issues = open_issues
        self.calls: list[tuple[str, str, dict | None]] = []

    def matching_open_issues(self) -> list[dict]:
        return self.open_issues

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        self.calls.append((method, path, payload))
        if method == "POST":
            return {"html_url": "https://github.com/owner/repository/issues/1"}
        return {}


class SyncIssueTests(unittest.TestCase):
    def base_env(self, status: str) -> dict[str, str]:
        return {
            "STOCK_STATUS": status,
            "GITHUB_REPOSITORY": "owner/repository",
            "GITHUB_TOKEN": "test-token",
            "ALERT_ASSIGNEE": "octocat",
        }

    def test_available_creates_one_assigned_issue(self) -> None:
        client = FakeGitHubClient([])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "output")
            with (
                mock.patch.dict(os.environ, self.base_env("available"), clear=True),
                mock.patch.object(
                    sync_github_issue, "GitHubClient", return_value=client
                ),
            ):
                code = sync_github_issue.main(
                    ["--github-output", str(output)]
                )

            self.assertEqual(code, 0)
            self.assertEqual(client.calls[0][0], "POST")
            self.assertEqual(client.calls[0][2]["assignees"], ["octocat"])
            self.assertEqual(
                client.calls[0][2]["title"],
                "✅ Monitored item is available",
            )
            self.assertNotIn("example.test", client.calls[0][2]["body"])
            self.assertIn("alert_created=true", output.read_text())

    def test_unavailable_closes_existing_alert(self) -> None:
        issue = {"number": 7, "html_url": "https://example.test/issues/7"}
        client = FakeGitHubClient([issue])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "output")
            with (
                mock.patch.dict(os.environ, self.base_env("unavailable"), clear=True),
                mock.patch.object(
                    sync_github_issue, "GitHubClient", return_value=client
                ),
            ):
                code = sync_github_issue.main(
                    ["--github-output", str(output)]
                )

            self.assertEqual(code, 0)
            self.assertEqual(client.calls[0][0], "PATCH")
            self.assertEqual(client.calls[0][2]["state"], "closed")
            self.assertIn("action=closed", output.read_text())


if __name__ == "__main__":
    unittest.main()
