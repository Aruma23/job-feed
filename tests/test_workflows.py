"""The workflows cannot be quietly made dangerous.   python -m unittest tests.test_workflows

2026's attacks on GitHub Actions came in three ways (claude-side notes: SECURITY.md, Nov 23): a third-party action's tag moved
to malicious code (tj-actions, Trivy, actions-cool), a workflow whose trigger or `${{ }}` text lets a stranger's branch name,
title or comment run as code (hackerbot-claw), and a stolen account pushing a new workflow that sends the secrets away
(GhostAction, Megalodon). This file pins the rules that close the first two and makes the third show up as a failing test:
every action pinned to a full commit, nothing granted by default, no trigger that runs strangers' code with our token, no
event text pasted into a shell, hashed dependencies only, and only the two secrets this repo has ever used.
Plain text checks (no YAML library: the feed installs only hashed requirements)."""
import os
import re
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(HERE, ".github", "workflows")
SECRETS_OK = {"GEMINI_API_KEY", "GITHUB_TOKEN"}       # adding one is a decision: change this line in the same review
EVENT_SAFE = re.compile(r"github\.event\.(pull_request\.(base|head)\.sha|before|after)\b")


def workflows():
    for n in sorted(os.listdir(WF)):
        if n.endswith((".yml", ".yaml")):
            with open(os.path.join(WF, n), encoding="utf-8") as f:
                yield n, f.read()


class Workflows(unittest.TestCase):
    def test_there_are_workflows(self):
        self.assertGreaterEqual(len(list(workflows())), 1)

    def test_every_action_is_pinned_to_a_full_commit(self):
        for n, text in workflows():
            for ref in re.findall(r"^\s*-?\s*uses:\s*([^\s#]+)", text, re.M):
                if ref.startswith("./"):
                    continue
                self.assertRegex(ref, r"@[0-9a-f]{40}$", f"{n}: {ref} is not pinned to a commit (a tag can be moved)")

    def test_nothing_is_granted_by_default(self):
        for n, text in workflows():
            self.assertRegex(text, r"(?m)^permissions:\s*\{\}\s*(#.*)?$", f"{n}: top-level permissions must be {{}}")
            self.assertNotRegex(text, r"(?m)^\s*permissions:\s*write-all", f"{n}: write-all")

    def test_no_trigger_runs_strangers_code_with_our_token(self):
        for n, text in workflows():
            self.assertNotRegex(text, r"(?m)^\s*pull_request_target\s*:", f"{n}: pull_request_target")
            self.assertNotRegex(text, r"(?m)^\s*(issue_comment|discussion_comment|pull_request_review_comment)\s*:", f"{n}: comment trigger")
            if re.search(r"(?m)^\s*workflow_run\s*:", text):   # ours: the watchdog after our own feed run; it must not check out that run's head
                self.assertNotRegex(text, r"github\.event\.workflow_run\.(head_sha|head_branch)", f"{n}: checks out the triggering run")

    def test_no_event_text_in_a_shell(self):
        for n, text in workflows():
            for expr in re.findall(r"\$\{\{\s*([^}]*)\}\}", text):
                if "github.event" in expr or "github.head_ref" in expr:
                    self.assertRegex(expr.strip(), EVENT_SAFE, f"{n}: ${{{{ {expr.strip()} }}}} puts outside text into the workflow")

    def test_dependencies_are_hashed(self):
        for n, text in workflows():
            for line in re.findall(r"(?m)^.*\bpip3? install\b.*$", text):
                self.assertIn("--require-hashes", line, f"{n}: {line.strip()}")
            self.assertNotRegex(text, r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh\b", f"{n}: a downloaded script piped into a shell")

    def test_only_the_known_secrets(self):
        for n, text in workflows():
            for s in re.findall(r"secrets\.([A-Za-z0-9_]+)", text):
                self.assertIn(s, SECRETS_OK, f"{n}: new secret {s}: a new workflow reading secrets is how GhostAction worked")


if __name__ == "__main__":
    unittest.main()
