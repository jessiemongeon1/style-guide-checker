# Style Guide Checker

Automated CI that audits `.mdx` documentation files against the [Sui Documentation Style Guide](https://docs.sui.io/references/contribute/style-guide) using Claude.

## How it works

1. A PR is opened or updated with `.mdx` file changes
2. The workflow sends each changed file + the style guide to Claude
3. Claude reports all violations (every rule is mandatory)
4. Results are posted as a PR comment
5. The job fails if any violations are found

## Setup

### 1. Add secrets

In **Settings > Secrets and variables > Actions > Secrets**:

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude |

`GITHUB_TOKEN` is provided automatically.

### 2. Required status check (optional)

To block merges on violations: **Settings > Branches > Branch protection rules > main** > enable "Require status checks" > add `Style guide audit`.

## Usage

### Automatic (on PRs)

Triggers automatically on any PR that changes `.mdx` files or anything under `docs/`.

### Manual (audit an external repo's PR)

Go to **Actions > Docs Style Guide Audit > Run workflow** and provide:
- **Repo**: `MystenLabs/sui` (or any repo)
- **PR number**: the PR to audit

### Local testing

```bash
export ANTHROPIC_API_KEY="sk-ant-..."

# Create a file list
echo "docs/content/some-page.mdx" > /tmp/files.txt

export CHANGED_FILES=/tmp/files.txt
export GITHUB_WORKSPACE=/path/to/repo

python scripts/style-guide-audit.py
```

## Files

```
style-guide-checker/
├── .github/workflows/docs-style-guide.yml   # CI workflow
├── scripts/style-guide-audit.py             # Audit script (sends files to Claude)
├── sui-documentation-style-guide.skill      # The style guide rules
└── README.md
```

## Customization

### Changing the style guide

Edit `sui-documentation-style-guide.skill`. The YAML frontmatter is stripped at runtime — only the markdown body is sent to Claude as the audit reference.

### Auditing different file types

Change the `paths` filter in the workflow and the `.endswith(".mdx")` check in the script.
