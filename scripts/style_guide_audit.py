"""
Docs Style Guide Audit

Reads changed .mdx files from a PR, sends each to Claude for review against
the Sui Documentation Style Guide skill, and generates a PR review comment
with findings and inline suggestions.
"""

import json
import os
import re
import sys
import zipfile
from pathlib import Path

import anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CHANGED_FILES = os.environ.get("CHANGED_FILES", "")
REPO_ROOT = os.environ.get("AUDIT_REPO_ROOT", os.environ.get("GITHUB_WORKSPACE", "."))

# Path to the style guide skill file (always in THIS repo, not the target repo)
_this_repo_root = os.environ.get("GITHUB_WORKSPACE", os.path.join(os.path.dirname(__file__), ".."))
SKILL_PATH = os.path.join(_this_repo_root, "sui-documentation-style-guide.skill")

# ---------------------------------------------------------------------------
# Load the style guide skill
# ---------------------------------------------------------------------------


def load_style_guide() -> str:
    """Load the style guide skill file content, stripping frontmatter.

    The .skill format is a zip archive containing a SKILL.md file.
    Falls back to reading as plain text if it's not a zip.
    """
    if zipfile.is_zipfile(SKILL_PATH):
        with zipfile.ZipFile(SKILL_PATH) as zf:
            # Find the SKILL.md inside the archive
            md_files = [n for n in zf.namelist() if n.endswith("SKILL.md")]
            if not md_files:
                raise FileNotFoundError(f"No SKILL.md found in {SKILL_PATH}")
            content = zf.read(md_files[0]).decode("utf-8")
    else:
        with open(SKILL_PATH) as f:
            content = f.read()

    # Strip YAML frontmatter (between --- delimiters)
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:].strip()

    return content


# ---------------------------------------------------------------------------
# Audit logic
# ---------------------------------------------------------------------------


def deterministic_checks(file_path: str, content: str) -> list[dict]:
    """Run regex-based style guide checks that don't need AI."""
    violations = []
    lines = content.split("\n")
    in_code_block = False

    for i, line in enumerate(lines):
        line_num = i + 1
        if re.match(r"^```", line):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        # Skip HTML/JSX lines
        if re.match(r"^\s*<", line) and not re.match(r"^\s*#{2,}", line):
            continue

        def in_backticks(text, match_str):
            idx = text.find(match_str)
            if idx == -1:
                return False
            count = text[:idx].count("`")
            return count % 2 == 1

        # Latin abbreviations
        for pattern, fix_word in [
            (r"\be\.g\.", "for example"),
            (r"\bi\.e\.", "that is"),
            (r"\betc\.", "and so on"),
        ]:
            m = re.search(pattern, line)
            if m and not in_backticks(line, m.group()):
                violations.append({
                    "line": line_num,
                    "rule": "No Latin abbreviations",
                    "current": m.group(),
                    "suggested": fix_word,
                })

        # "via" → "through"
        m = re.search(r"\bvia\b", line, re.IGNORECASE)
        if m and not in_backticks(line, m.group()):
            violations.append({
                "line": line_num, "rule": 'Use "through" not "via"',
                "current": m.group(), "suggested": "through",
            })

        # "simple"/"simply"
        m = re.search(r"\b(simple|simply)\b", line, re.IGNORECASE)
        if m and not in_backticks(line, m.group()):
            violations.append({
                "line": line_num, "rule": 'Use "basic" not "simple"',
                "current": m.group(),
                "suggested": "basic" if m.group().lower() == "simple" else "(remove)",
            })

        # "dApp"/"dApps"
        m = re.search(r"\b(dApp|dApps)\b", line)
        if m and not in_backticks(line, m.group()):
            violations.append({
                "line": line_num, "rule": 'Use "app" not "dApp"',
                "current": m.group(),
                "suggested": "app" if m.group() == "dApp" else "apps",
            })

        # "may" → "might"
        if re.search(r"\bmay\b", line) and not in_backticks(line, "may") and not re.match(r"^\s*#", line):
            violations.append({
                "line": line_num, "rule": 'Use "might" not "may"',
                "current": "may", "suggested": "might",
            })

        # Em dashes
        if "—" in line and not in_backticks(line, "—"):
            violations.append({
                "line": line_num, "rule": "No em dashes",
                "current": "—", "suggested": "Rewrite with comma, parentheses, or split sentence",
            })

        # Ampersands in prose
        if " & " in line and "&amp;" not in line and "&lt;" not in line:
            violations.append({
                "line": line_num, "rule": 'Use "and" not "&"',
                "current": "&", "suggested": "and",
            })

        # Lowercase network names
        for lower, upper in [("mainnet", "Mainnet"), ("testnet", "Testnet"),
                             ("devnet", "Devnet"), ("localnet", "Localnet")]:
            if re.search(rf"\b{lower}\b", line) and not in_backticks(line, lower):
                violations.append({
                    "line": line_num, "rule": f"Capitalize {lower}",
                    "current": lower, "suggested": upper,
                })

        # "Note that" / "Please note"
        if re.match(r"^\s*(Note that|Please note)", line, re.IGNORECASE):
            violations.append({
                "line": line_num, "rule": "No 'Note that'/'Please note' at sentence start",
                "current": line.strip()[:50], "suggested": "Remove or rewrite",
            })

        # Exclamation marks
        if re.search(r"[^`!]![^\[\]`]", line) and "Hello, World!" not in line and not re.match(r"^\s*[!<]", line):
            violations.append({
                "line": line_num, "rule": "No exclamation marks",
                "current": "!", "suggested": ".",
            })

        # Section heading sentence case
        heading = re.match(r"^(#{2,5})\s+(.+)", line)
        if heading:
            text = heading.group(2)
            words = re.sub(r"`[^`]+`", "", text).split()
            proper = {"Sui", "Move", "React", "Vue", "TypeScript", "JavaScript", "GraphQL",
                      "JSON", "gRPC", "API", "SDK", "CLI", "BCS", "ID", "Mainnet", "Testnet",
                      "Devnet", "Localnet", "DeepBook", "Kiosk", "Walrus", "SUI", "SSR",
                      "WASM", "OAuth", "OpenID", "Step", "One-Time", "Witness"}
            caps = sum(1 for w in words[1:] if len(w) > 2 and w[0].isupper() and w not in proper)
            if caps >= 2:
                violations.append({
                    "line": line_num, "rule": "Headings use sentence case",
                    "current": text, "suggested": "Capitalize only first word and proper nouns",
                })

            # Period at end of heading
            if text.strip().endswith("."):
                violations.append({
                    "line": line_num, "rule": "No period after heading",
                    "current": text, "suggested": text.rstrip("."),
                })

        # First person
        if re.search(r"\b(we |we'|our |let's |let us )", line, re.IGNORECASE) and not in_backticks(line, "we") and not re.match(r"^\s*#", line):
            violations.append({
                "line": line_num, "rule": "Use second person (you), not first person",
                "current": line.strip()[:50], "suggested": "Rewrite using 'you'",
            })

        # Future tense "will"
        if re.search(r"\bwill\b", line) and not in_backticks(line, "will") and not re.match(r"^\s*#", line):
            violations.append({
                "line": line_num, "rule": "Use present tense, not future",
                "current": "will", "suggested": "Use present tense verb",
            })

        # British spellings
        for british, american in [
            ("behaviour", "behavior"), ("colour", "color"), ("favour", "favor"),
            ("initialise", "initialize"), ("serialise", "serialize"),
            ("customise", "customize"), ("organise", "organize"),
            ("factorisation", "factorization"),
        ]:
            if re.search(rf"\b{british}\b", line, re.IGNORECASE) and not in_backticks(line, british):
                violations.append({
                    "line": line_num, "rule": f"US English: {british} → {american}",
                    "current": british, "suggested": american,
                })

        # H1 in body (should only be in frontmatter)
        if re.match(r"^#\s+", line) and not re.match(r"^##", line):
            violations.append({
                "line": line_num, "rule": "H1 only in frontmatter",
                "current": line.strip()[:60], "suggested": "Use ## or lower for section headings",
            })

        # on-chain/off-chain → onchain/offchain
        for wrong, right in [("off-chain", "offchain"), ("on-chain", "onchain"),
                             ("off chain", "offchain"), ("on chain", "onchain")]:
            if re.search(rf"\b{wrong}\b", line, re.IGNORECASE) and not in_backticks(line, wrong):
                violations.append({
                    "line": line_num, "rule": f'"{wrong}" is one word → "{right}"',
                    "current": wrong, "suggested": right,
                })

        # Term lists using dash instead of colon (bold terms)
        if re.match(r"^\s*[-*]\s+\*\*[^*]+\*\*\s+-\s+", line):
            violations.append({
                "line": line_num, "rule": "Term lists use colon, not dash",
                "current": "**Term** - def", "suggested": "**Term:** def",
            })

        # Attribute/term lists using dash instead of colon (code or plain terms)
        if re.match(r"^\s*[-*]\s+`[^`]+`\s+-\s+", line):
            violations.append({
                "line": line_num, "rule": "Attribute lists use colon, not dash",
                "current": "`attr` - desc", "suggested": "`attr`: desc",
            })

        # Plain term lists using dash (non-bold, non-code)
        if re.match(r"^\s*[-*]\s+\S+\s+-\s+", line) and not re.match(r"^\s*[-*]\s+[`*]", line):
            violations.append({
                "line": line_num, "rule": "List items use colon, not dash, for definitions",
                "current": line.strip()[:60], "suggested": "Use colon: `- Term: Definition`",
            })

        # Causal "since"
        if re.search(r"(?:^|\.\s+|,\s+)[Ss]ince\s+", line) and not in_backticks(line, "since"):
            violations.append({
                "line": line_num, "rule": 'Use "because" not causal "since"',
                "current": "since", "suggested": "because",
            })

    # Frontmatter checks
    fm = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if fm:
        fmtext = fm.group(1)
        if "description:" not in fmtext or re.search(r"description:\s*(placeholder)?\s*$", fmtext, re.MULTILINE):
            violations.append({
                "line": 1, "rule": "Frontmatter needs description",
                "current": "(missing or placeholder)", "suggested": "Add a complete sentence description",
            })
        if "keywords:" not in fmtext or re.search(r"keywords:\s*\[\s*placeholder\s*\]", fmtext):
            violations.append({
                "line": 1, "rule": "Frontmatter needs keywords",
                "current": "(missing or placeholder)", "suggested": "Add relevant keywords",
            })

    # Admonition count
    adm_count = len(re.findall(r"^:::(caution|danger|info|note|tip)", content, re.MULTILINE))
    if adm_count > 4:
        violations.append({
            "line": 1, "rule": f"Max 4 admonitions per page (found {adm_count})",
            "current": f"{adm_count} admonitions", "suggested": "Remove or consolidate",
        })

    return violations


def claude_review(file_path: str, content: str, style_guide: str,
                   existing_rules: set[str]) -> list[dict]:
    """Use Claude to catch nuanced style guide violations that regex misses.

    Args:
        file_path: Path to the file being audited.
        content: The file content.
        style_guide: The full style guide text.
        existing_rules: Set of rule names already flagged by regex checks,
                        so Claude doesn't duplicate them.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("    Skipping Claude review: ANTHROPIC_API_KEY not set")
        return []

    client = anthropic.Anthropic(api_key=api_key)

    rules_already_covered = "\n".join(f"- {r}" for r in sorted(existing_rules))

    prompt = f"""You are a strict documentation style guide auditor. Review the following .mdx file against the style guide below.

IMPORTANT RULES FOR YOUR REVIEW:
1. Only flag CLEAR, UNAMBIGUOUS violations of the style guide. If you are not at least 90% confident something is a violation, do NOT flag it.
2. Do NOT flag issues inside code blocks (``` ... ```) or inline code (`...`).
3. Do NOT flag issues inside HTML/JSX tags.
4. Do NOT duplicate violations already caught by regex checks. The following rules are ALREADY covered — skip them entirely:
{rules_already_covered}
5. Focus on nuanced issues regex cannot catch, such as:
   - Tone problems (condescending, overly casual, or marketing language)
   - Instructions that use passive voice when active voice would be clearer
   - Unclear or ambiguous phrasing that could confuse the reader
   - Incorrect technical terminology per the style guide
   - Structural issues (e.g., missing prerequisites, steps out of order)
6. Return ONLY a JSON array of violations. Each violation must have exactly these fields:
   - "line": the line number (integer)
   - "rule": a short rule name (string)
   - "current": the problematic text, max 100 chars (string)
   - "suggested": a concrete fix or "(rewrite)" if the fix is complex (string)
7. If there are NO violations, return an empty array: []
8. Do NOT include explanations, preamble, or markdown formatting. Return ONLY the JSON array.

<style_guide>
{style_guide}
</style_guide>

<file path="{file_path}">
{content}
</file>

Return ONLY the JSON array:"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text

        text = text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        violations = json.loads(text)
        if not isinstance(violations, list):
            print(f"    Claude returned non-list: {type(violations)}")
            return []

        # Validate each violation has required fields
        valid = []
        for v in violations:
            if (isinstance(v, dict) and "line" in v and "rule" in v
                    and "current" in v and "suggested" in v):
                # Skip if the suggested fix is essentially "no change"
                if v["suggested"].lower() in ("no change needed", "no change", "n/a"):
                    continue
                v["source"] = "claude"
                valid.append(v)

        return valid

    except anthropic.APIError as e:
        print(f"    Claude API error: {e}")
        return []
    except (json.JSONDecodeError, KeyError) as e:
        print(f"    Failed to parse Claude response: {e}")
        return []


def audit_file(file_path: str, style_guide: str) -> dict:
    """Audit a single file against the style guide.

    Runs deterministic regex checks first, then uses Claude
    for nuanced issues that regex can't catch.
    """
    abs_path = os.path.join(REPO_ROOT, file_path)
    print(f"    REPO_ROOT={REPO_ROOT}")
    print(f"    abs_path={abs_path}")
    print(f"    exists={os.path.exists(abs_path)}")

    if not os.path.exists(abs_path):
        return {
            "file": file_path,
            "summary": "File not found",
            "violations": [],
        }

    with open(abs_path) as f:
        content = f.read()

    print(f"    content length={len(content)}")
    print(f"    first 200 chars: {repr(content[:200])}")

    # Run deterministic checks first — these always catch violations
    regex_violations = deterministic_checks(file_path, content)
    print(f"    regex violations={len(regex_violations)}")

    # Run Claude review for nuanced issues regex can't catch
    existing_rules = {v["rule"] for v in regex_violations}
    claude_violations = claude_review(file_path, content, style_guide, existing_rules)
    print(f"    claude violations={len(claude_violations)}")

    all_violations = regex_violations + claude_violations

    return {
        "file": file_path,
        "summary": f"{len(all_violations)} violation(s) ({len(regex_violations)} regex, {len(claude_violations)} claude)",
        "violations": all_violations,
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_review_comment(results: list[dict]) -> str:
    """Generate a markdown PR review comment from audit results."""
    marker = "<!-- style-guide-audit -->"

    total_violations = sum(len(r.get("violations", [])) for r in results)

    if total_violations == 0:
        return f"{marker}\n## Style Guide Audit\n\nAll {len(results)} file(s) pass the style guide audit."

    lines = [
        marker,
        "## Style Guide Audit",
        "",
        f"Audited **{len(results)}** file(s) against the "
        f"[Sui Documentation Style Guide](https://docs.sui.io/references/contribute/style-guide).",
        "",
        f"**{total_violations}** violation(s) found. All must be fixed before merge.",
        "",
    ]

    for result in results:
        file_path = result.get("file", "unknown")
        violations = result.get("violations", [])

        if not violations:
            continue

        lines.append(f"### `{file_path}` ({len(violations)} violation(s))")
        lines.append("")
        if result.get("summary"):
            lines.append(f"_{result['summary']}_")
            lines.append("")

        for item in violations:
            lines.append(f"- **Line {item.get('line', '?')}** — {item.get('rule', '')}")
            lines.append(f"  - Current: `{item.get('current', '')[:150]}`")
            lines.append(f"  - Fix: `{item.get('suggested', '')[:150]}`")
        lines.append("")

    lines.append("---")
    lines.append("_Automated audit using the Sui Documentation Style Guide._")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=== Docs Style Guide Audit ===")

    # Load style guide
    print(f"Loading style guide from: {SKILL_PATH}")
    if not os.path.exists(SKILL_PATH):
        print(f"ERROR: Style guide skill file not found at {SKILL_PATH}")
        sys.exit(1)

    style_guide = load_style_guide()
    print(f"Style guide loaded ({len(style_guide)} chars)")

    # Read changed files
    if not CHANGED_FILES or not os.path.exists(CHANGED_FILES):
        print("No changed files to audit")
        return

    with open(CHANGED_FILES) as f:
        files = [line.strip() for line in f if line.strip() and line.strip().endswith(".mdx")]

    if not files:
        print("No .mdx files to audit")
        return

    print(f"\nAuditing {len(files)} file(s):")
    for f in files:
        print(f"  - {f}")

    # Audit each file
    results = []
    for file_path in files:
        print(f"\nAuditing: {file_path}")
        result = audit_file(file_path, style_guide)
        results.append(result)

        count = len(result.get("violations", []))
        print(f"  {count} violation(s)")

    # Generate review comment
    review = generate_review_comment(results)

    # Write to file for the workflow to post
    output_path = "/tmp/audit_review.md"
    with open(output_path, "w") as f:
        f.write(review)
    print(f"\nReview written to {output_path}")

    # Summary
    total = sum(len(r.get("violations", [])) for r in results)
    print(f"\nTotal: {total} violation(s)")

    if total > 0:
        print(f"\n{total} style guide violation(s) found — see PR comment for details")


if __name__ == "__main__":
    main()
