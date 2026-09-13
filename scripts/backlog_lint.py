"""Pre-commit lint for docs/backlog_cache.md and docs/deep_backlog.md.

Only checks NEWLY ADDED `## ` header lines in the staged diff -- deliberately does
NOT validate the whole file, since real historical entries use legitimately
different shapes (untagged "standing convention" headers, approximate dates like
"2026-08-1x", revisit-only headers with no creation date at all). Retroactively
enforcing a strict schema against ~600 hand-written entries accumulated over
months would either reject a pile of valid history or require a one-time cleanup
pass first. Scoping to new lines only lets the hook go live immediately and just
stops NEW drift, per the tag-vocabulary problem found 2026-09-12 (94 distinct tags
across both files, many single-use/likely typos from "many generations" of
sessions writing headers by hand with no shared reference list).

Hard-fails only on unknown tags (the concrete, measurable problem) -- missing
tags/date on a new header are warnings, not blockers, since untagged headers are
still a legitimate style (standing conventions/default rules) that shouldn't be
forced into a tag scheme it doesn't need.
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_backlog_cache_lean

FILES = ["docs/backlog_cache.md", "docs/deep_backlog.md"]

# Real vocabulary as observed 2026-09-12 (grep across both files), plus the full
# severity tier (only [HIGH] has been used so far, but [CRITICAL]/[MEDIUM]/[LOW]
# are the natural completion of that scheme and shouldn't get flagged the first
# time someone actually uses one).
KNOWN_TAGS = {
    "live-trading", "backtest", "security", "tooling", "coverage", "testing",
    "portfolio", "data", "tax", "specced", "design", "new-strategy", "process",
    "ops", "execution", "docs", "research", "meta", "compliance", "test-infra",
    "state", "risk", "performance", "monitoring",
    "HIGH", "CRITICAL", "MEDIUM", "LOW",
}
# "action" deliberately excluded (reviewed 2026-09-12): its one historical use
# (docs/deep_backlog.md:1518, [data][action]) reads as a stray status-ish label
# from that session's own ad hoc convention, not a real recurring topic like the
# others above -- nothing else in either file ever reused it. The historical
# entry itself is left as-is (grandfathered text, not retroactively edited); this
# just stops a future session from treating it as an established category.

HEADER_RE = re.compile(r"^## (?:✅ )?((?:\[[^\]]+\])*)")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{4}-\d{2}-\d?x|revisit")


def get_added_header_lines(path):
    """Returns newly-added '## ' header lines from the staged diff for `path`,
    or [] if the file isn't staged/changed."""
    try:
        diff = subprocess.run(
            ["git", "diff", "--cached", "-U0", "--", path],
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return []
    added = []
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("+  ") or not line.startswith("+"):
            continue
        content = line[1:]
        if content.startswith("## "):
            added.append(content)
    return added


def check_line(path, line):
    """Returns (errors, warnings) for one new header line."""
    errors, warnings = [], []
    m = HEADER_RE.match(line)
    if not m:
        return errors, warnings  # not a real header shape we track, ignore
    tag_block = m.group(1)
    tags = re.findall(r"\[([^\]]+)\]", tag_block)
    if not tags:
        warnings.append(f"{path}: no tags on new header (allowed, but unusual): {line[:100]}")
    else:
        for t in tags:
            if t not in KNOWN_TAGS:
                errors.append(
                    f"{path}: unknown tag '[{t}]' on new header (not in the known "
                    f"vocabulary -- add it to KNOWN_TAGS in scripts/backlog_lint.py "
                    f"if this is a deliberate new category, otherwise fix the typo): "
                    f"{line[:100]}"
                )
    if not DATE_RE.search(line):
        warnings.append(f"{path}: no date/revisit marker found on new header: {line[:100]}")
    return errors, warnings


def check_new_entries_lean(path="docs/backlog_cache.md", max_lines=2):
    """Flags NEWLY-ADDED backlog_cache.md entries that exceed the file's own
    1-2-line pointer convention -- reuses check_backlog_cache_lean.py's own
    violation-finder against the current file content, then only reports
    violations whose header was actually added in this commit's diff (13
    pre-existing entries already exceed this convention as of 2026-09-12;
    grandfathered rather than blocking every future commit on old debt)."""
    new_headers = set(get_added_header_lines(path))
    if not new_headers:
        return []
    text = Path(path).read_text()
    violations = check_backlog_cache_lean.find_violations(text, max_lines)
    errors = []
    for header, n in violations:
        if header in new_headers:
            errors.append(
                f"{path}: new entry exceeds the {max_lines}-line pointer convention "
                f"({n} lines) -- relocate the full write-up to deep_backlog.md, leave "
                f"a 1-2 line pointer here: {header[:100]}"
            )
    return errors


def main():
    all_errors, all_warnings = [], []
    for path in FILES:
        for line in get_added_header_lines(path):
            errors, warnings = check_line(path, line)
            all_errors.extend(errors)
            all_warnings.extend(warnings)
    all_errors.extend(check_new_entries_lean())

    for w in all_warnings:
        print(f"[backlog-lint WARN] {w}", file=sys.stderr)

    if all_errors:
        for e in all_errors:
            print(f"[backlog-lint FAIL] {e}", file=sys.stderr)
        print(
            f"\n{len(all_errors)} backlog header tag error(s) -- fix the tag(s) above, "
            f"or add a genuinely new tag to KNOWN_TAGS in scripts/backlog_lint.py.\n"
            f"Bypass (not recommended): git commit --no-verify",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    sys.exit(main())
