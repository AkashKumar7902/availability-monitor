# Private-target availability monitor

This public repository checks one privately configured item every five minutes
with GitHub Actions. The target URL and identifiers live only in encrypted
repository secrets: they are not committed, printed in workflow logs, written to
job summaries, or included in alert issues.

When availability is confirmed twice, the workflow creates a generic issue and
assigns it to the repository owner. GitHub can then notify that person by web,
email, or mobile. The issue closes automatically if the item becomes unavailable
again, and a later restock creates a new alert.

## Privacy model

The repository deliberately keeps these details private:

- API endpoint
- item ID
- item SKU
- item slug
- retailer and item name

The public workflow reveals only `available`, `unavailable`, or `unknown`. The
alert says only that the privately configured item is available. Anyone can read
the generic source and workflow history, but repository readers cannot retrieve
Actions secret values.

Do not replace the secrets with repository variables or literal values. Do not
add the private target to issue text, commit messages, test fixtures, workflow
names, or job output.

## Configure the four secrets

In **Settings → Secrets and variables → Actions**, create these repository
secrets:

| Secret | Private value |
|---|---|
| `MONITOR_API_URL` | Complete JSON API endpoint for the target item |
| `MONITOR_PRODUCT_ID` | Expected numeric item ID |
| `MONITOR_PRODUCT_SKU` | Expected SKU |
| `MONITOR_PRODUCT_SLUG` | Expected URL/API slug |

They can also be entered interactively with GitHub CLI, which avoids putting a
value directly in the command itself:

```bash
gh secret set MONITOR_API_URL
gh secret set MONITOR_PRODUCT_ID
gh secret set MONITOR_PRODUCT_SKU
gh secret set MONITOR_PRODUCT_SLUG
```

The alert defaults to the user who runs the workflow. To choose another
collaborator, create a non-sensitive repository variable named
`ALERT_ASSIGNEE` containing their GitHub username.

After setting the secrets, open **Actions → Private-target availability monitor**
and select **Run workflow** once.

## Expected API shape

The checker expects a response shaped like this example:

```json
{
  "status": 1,
  "product": {
    "id": 42,
    "sku_code": "EXAMPLE-SKU",
    "slug": "example-item",
    "stock_count": 0,
    "delivery_stock": 0,
    "not_for_sale": "No",
    "status": "ACTIVE"
  }
}
```

It reports `available` only when the response is successful, the private ID/SKU/
slug match, the item is active and for sale, and either stock field is positive.
A blocked request, timeout, identity mismatch, malformed JSON, or schema change
becomes `unknown` and fails visibly; it is never silently treated as unavailable.
A positive result is fetched again after ten seconds before an alert is created.

## Permissions and notifications

The workflow uses GitHub's built-in, short-lived token and requests only:

```yaml
permissions:
  contents: read
  issues: write
```

The resulting issue is public but contains no target information. Enable
assigned-issue notifications in GitHub email or mobile settings to receive the
alert promptly.

## Timing and cost

The cron runs at minutes `2, 7, 12, ... 57`, avoiding the busiest exact start of
the hour. Detection normally takes zero to five minutes plus any GitHub queue
delay. Scheduled Actions are best-effort and are not an instantaneous webhook.

Standard GitHub-hosted runners are free for public repositories. GitHub may
disable schedules in a public repository after 60 days without repository
activity.

## Test locally

Python 3.10+ is enough; there are no third-party packages.

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py tests/*.py
```

To perform a live local check, provide all four private configuration values in
the environment before running `python3 scripts/check_stock.py`. Keep them out of
shell history and untracked files.

## Files

```text
.github/workflows/stock-monitor.yml  Five-minute schedule and secret wiring
scripts/check_stock.py               API validation and availability classifier
scripts/sync_github_issue.py         Generic, idempotent GitHub issue alert
tests/                               Non-sensitive fixtures and tests
```
