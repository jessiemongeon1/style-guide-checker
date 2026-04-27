"""
Find and Audit PRs

Polls monitored repos for open PRs that touch .mdx files,
runs the style guide audit on each, and posts results as PR comments.
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Repos to monitor — add new repos here
# ---------------------------------------------------------------------------

MONITORED_REPOS = [
    {
        "repo": "MystenLabs/sui",
        "docs_paths": ["docs/content/"],
    },
    {
        "repo": "MystenLabs/walrus",
        "docs_paths": ["docs/content/"],
    },
    {
        "repo": "MystenLabs/seal",
        "docs_paths": ["docs/content/"],
    },
    {
        "repo": "MystenLabs/suins-contracts",
        "docs_paths": ["documentation/content/"],
    },
    {
        "repo": "MystenLabs/move-book",
        "docs_paths": ["book/"],
    },
]

# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

STATE_DIR = Path(__file__).parent.parent / "state"
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "audited_prs.json"


def load_audited() -> dict[str, list[str]]:
    """Load {repo: [pr_sha, ...]} of already-audited PR+SHA combos."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, KeyError):
        return {}


def save_audited(state: dict[str, list[str]]):
    # Keep only last 200 entries per repo
    trimmed = {repo: shas[-200:] for repo, shas in state.items()}
    STATE_FILE.write_text(json.dumps(trimmed, indent=2) + "\n")


def pr_key(pr_number: int, head_sha: str) -> str:
    """Unique key for a PR at a specific commit."""
    return f"{pr_number}:{head_sha}"


# ---------------------------------------------------------------------------
# GitHub helpers (using gh CLI)
# ---------------------------------------------------------------------------


def run_gh(args: list[str]) -> str:
    """Run a gh CLI command and return stdout."""
    result = subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  gh {' '.join(args[:3])}... failed: {result.stderr.strip()}")
        return ""
    return result.stdout.strip()


def get_open_prs(repo: str) -> list[dict]:
    """Get open, non-draft PRs for a repo."""
    output = run_gh([
        "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number,headRefOid,title,isDraft",
        "--limit", "50",
    ])
    if not output:
        return []
    try:
        prs = json.loads(output)
        return [pr for pr in prs if not pr.get("isDraft")]
    except json.JSONDecodeError:
        return []


def get_pr_changed_files(repo: str, pr_number: int) -> list[str]:
    """Get list of changed files in a PR."""
    output = run_gh([
        "pr", "diff", str(pr_number),
        "--repo", repo,
        "--name-only",
    ])
    if not output:
        return []
    return output.strip().split("\n")


def has_mdx_changes(changed_files: list[str], docs_paths: list[str]) -> list[str]:
    """Filter to .mdx files within the docs paths."""
    mdx_files = []
    for f in changed_files:
        if not f.endswith(".mdx"):
            continue
        # If docs_paths is specified, only include files under those paths
        if docs_paths:
            if any(f.startswith(prefix) for prefix in docs_paths):
                mdx_files.append(f)
        else:
            mdx_files.append(f)
    return mdx_files


def clone_pr_branch(repo: str, pr_number: int, dest: str) -> bool:
    """Checkout the PR's head into dest."""
    result = subprocess.run(
        ["gh", "pr", "checkout", str(pr_number), "--repo", repo, "--detach"],
        capture_output=True,
        text=True,
        cwd=dest,
    )
    return result.returncode == 0


def post_or_update_comment(repo: str, pr_number: int, body: str):
    """Post or update the style guide audit comment on a PR."""
    marker = "<!-- style-guide-audit -->"

    # Find existing comment
    existing = run_gh([
        "api", f"repos/{repo}/issues/{pr_number}/comments",
        "--paginate",
        "--jq", f'.[] | select(.body | contains("{marker}")) | .id',
    ])
    existing_id = existing.strip().split("\n")[0] if existing.strip() else ""

    if existing_id:
        run_gh([
            "api", f"repos/{repo}/issues/comments/{existing_id}",
            "-X", "PATCH",
            "-f", f"body={body}",
        ])
        print(f"    Updated comment on PR #{pr_number}")
    else:
        run_gh([
            "api", f"repos/{repo}/issues/{pr_number}/comments",
            "-X", "POST",
            "-f", f"body={body}",
        ])
        print(f"    Posted comment on PR #{pr_number}")


def notify_slack(repo: str, pr_number: int, pr_title: str, total_violations: int):
    """Post a summary to Slack via incoming webhook."""
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        print("    Skipping Slack notification: SLACK_WEBHOOK_URL not set")
        return

    pr_url = f"https://github.com/{repo}/pull/{pr_number}"
    text = (
        f"*Style Guide Audit:* <{pr_url}|{repo}#{pr_number}> — {pr_title}\n"
        f"Total edits needed: *{total_violations}*"
    )

    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req)
        print(f"    Slack notification sent for PR #{pr_number}")
    except Exception as e:
        print(f"    Slack notification failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=== Style Guide PR Monitor ===\n")

    # Import the audit functions from the sibling script
    sys.path.insert(0, str(Path(__file__).parent))
    import style_guide_audit as audit_mod

    style_guide = audit_mod.load_style_guide()
    print(f"Style guide loaded ({len(style_guide)} chars)\n")

    state = load_audited()
    new_audits = False

    for config in MONITORED_REPOS:
        repo = config["repo"]
        docs_paths = config.get("docs_paths", [])
        print(f"Checking {repo}...")

        repo_state = state.get(repo, [])
        prs = get_open_prs(repo)

        if not prs:
            print(f"  No open PRs\n")
            continue

        print(f"  {len(prs)} open PR(s)")

        for pr in prs:
            pr_num = pr["number"]
            head_sha = pr["headRefOid"]
            key = pr_key(pr_num, head_sha)

            if key in repo_state:
                continue

            # Check if this PR has .mdx changes in docs paths
            changed = get_pr_changed_files(repo, pr_num)
            mdx_files = has_mdx_changes(changed, docs_paths)

            if not mdx_files:
                # Mark as audited (no docs changes) so we don't re-check
                repo_state.append(key)
                continue

            print(f"\n  PR #{pr_num}: {pr['title'][:60]}")
            print(f"    {len(mdx_files)} .mdx file(s) to audit")

            # Clone the repo at the PR's head SHA directly
            clone_dir = f"/tmp/audit-{repo.replace('/', '-')}-{pr_num}"
            subprocess.run(["rm", "-rf", clone_dir], capture_output=True)

            # Fetch the PR head commit directly — gh pr checkout doesn't work
            # reliably on shallow clones, so we clone at the exact SHA instead.
            clone_result = subprocess.run(
                ["git", "clone", "--depth=1",
                 f"https://github.com/{repo}.git", clone_dir],
                capture_output=True, text=True,
            )
            if clone_result.returncode != 0:
                print(f"    Failed to clone {repo}: {clone_result.stderr.strip()}")
                repo_state.append(key)
                continue

            # Fetch the PR ref and checkout
            fetch_result = subprocess.run(
                ["git", "-C", clone_dir, "fetch", "origin",
                 f"pull/{pr_num}/head:pr-{pr_num}"],
                capture_output=True, text=True,
            )
            if fetch_result.returncode != 0:
                print(f"    Failed to fetch PR #{pr_num}: {fetch_result.stderr.strip()}")
                repo_state.append(key)
                continue

            checkout_result = subprocess.run(
                ["git", "-C", clone_dir, "checkout", f"pr-{pr_num}"],
                capture_output=True, text=True,
            )
            if checkout_result.returncode != 0:
                print(f"    Failed to checkout PR #{pr_num}: {checkout_result.stderr.strip()}")
                repo_state.append(key)
                continue

            print(f"    Checked out PR #{pr_num} at {head_sha[:8]}")

            # Write the file list
            file_list_path = f"/tmp/mdx_files_{pr_num}.txt"
            with open(file_list_path, "w") as f:
                f.write("\n".join(mdx_files))

            # Run audit
            results = []
            for mdx_file in mdx_files:
                print(f"    Auditing: {mdx_file}")
                # Temporarily set REPO_ROOT for the audit function
                old_root = audit_mod.REPO_ROOT
                audit_mod.REPO_ROOT = clone_dir
                result = audit_mod.audit_file(mdx_file, style_guide)
                audit_mod.REPO_ROOT = old_root
                results.append(result)

                count = len(result.get("violations", []))
                print(f"      {count} violation(s)")

            # Generate and post comment
            total_violations = sum(len(r.get("violations", [])) for r in results)
            review = audit_mod.generate_review_comment(results)
            post_or_update_comment(repo, pr_num, review)
            notify_slack(repo, pr_num, pr["title"], total_violations)

            # Mark as audited
            repo_state.append(key)
            new_audits = True

            # Cleanup
            subprocess.run(["rm", "-rf", clone_dir], capture_output=True)

        state[repo] = repo_state

    save_audited(state)
    if new_audits:
        print("\nNew audits completed and state saved.")
    else:
        print("\nNo new PRs to audit.")


if __name__ == "__main__":
    main()
