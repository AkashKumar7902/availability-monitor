from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import sync_github_issue


class FakeGitHubClient:
    repository_path = "owner/repository"

    def __init__(self, open_issues: dict[str, list[dict]] | None = None) -> None:
        self.open_issues = open_issues or {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.requested_markers: list[str] = []

    def matching_open_issues(self, marker: str, title: str) -> list[dict]:
        self.requested_markers.append(marker)
        return self.open_issues.get(marker, [])

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
        client = FakeGitHubClient()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "output")
            with (
                mock.patch.dict(os.environ, self.base_env("available"), clear=True),
                mock.patch.object(
                    sync_github_issue, "GitHubClient", return_value=client
                ),
            ):
                code = sync_github_issue.main(
                    ["--slot", "1", "--github-output", str(output)]
                )

            self.assertEqual(code, 0)
            self.assertEqual(client.calls[0][0], "POST")
            self.assertEqual(client.calls[0][2]["assignees"], ["octocat"])
            self.assertEqual(
                client.calls[0][2]["title"],
                sync_github_issue.issue_title("1"),
            )
            self.assertIn(sync_github_issue.MARKERS["1"], client.calls[0][2]["body"])
            self.assertNotIn("example.test", client.calls[0][2]["body"])
            self.assertIn("alert_created=true", output.read_text())

    def test_unavailable_closes_only_the_selected_slot(self) -> None:
        item_1 = {"number": 7, "html_url": "https://example.test/issues/7"}
        item_2 = {"number": 8, "html_url": "https://example.test/issues/8"}
        client = FakeGitHubClient(
            {
                sync_github_issue.MARKERS["1"]: [item_1],
                sync_github_issue.MARKERS["2"]: [item_2],
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "output")
            with (
                mock.patch.dict(os.environ, self.base_env("unavailable"), clear=True),
                mock.patch.object(
                    sync_github_issue, "GitHubClient", return_value=client
                ),
            ):
                code = sync_github_issue.main(
                    ["--slot", "2", "--github-output", str(output)]
                )

            self.assertEqual(code, 0)
            self.assertEqual(
                client.requested_markers,
                [sync_github_issue.MARKERS["2"]],
            )
            self.assertEqual(client.calls[0][0], "PATCH")
            self.assertIn("/issues/8", client.calls[0][1])
            self.assertEqual(client.calls[0][2]["state"], "closed")
            self.assertIn("action=closed", output.read_text())

    def test_item_2_alert_is_generic(self) -> None:
        client = FakeGitHubClient()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "output")
            with (
                mock.patch.dict(os.environ, self.base_env("available"), clear=True),
                mock.patch.object(
                    sync_github_issue, "GitHubClient", return_value=client
                ),
            ):
                sync_github_issue.main(
                    ["--slot", "2", "--github-output", str(output)]
                )

        payload = client.calls[0][2]
        self.assertEqual(payload["title"], sync_github_issue.issue_title("2"))
        self.assertIn(sync_github_issue.MARKERS["2"], payload["body"])
        self.assertNotIn("http", payload["body"])

    def test_real_issue_filter_rejects_spoofed_and_other_slot_issues(self) -> None:
        client = sync_github_issue.GitHubClient(
            "https://api.example.test",
            "owner/repository",
            "test-token",
        )
        trusted = {
            "number": 1,
            "title": sync_github_issue.issue_title("1"),
            "body": sync_github_issue.MARKERS["1"],
            "user": {"login": "github-actions[bot]"},
        }
        spoofed = {
            "number": 2,
            "title": sync_github_issue.issue_title("1"),
            "body": sync_github_issue.MARKERS["1"],
            "user": {"login": "someone-else"},
        }
        other_slot = {
            "number": 3,
            "title": sync_github_issue.issue_title("2"),
            "body": sync_github_issue.MARKERS["2"],
            "user": {"login": "github-actions[bot]"},
        }
        with mock.patch.object(
            client,
            "request",
            return_value=[trusted, spoofed, other_slot],
        ):
            matches = client.matching_open_issues(
                sync_github_issue.MARKERS["1"],
                sync_github_issue.issue_title("1"),
            )

        self.assertEqual(matches, [trusted])


if __name__ == "__main__":
    unittest.main()
