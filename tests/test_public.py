"""This repository is public: its code, tests, docs and workflows identify no one.   python -m unittest tests.test_public

A lint, not a secret list: it cannot know anyone's name (a list of forbidden names, hashed or not, would itself name them),
so it checks for the shapes that gave things away before: lists of digests, personal mail addresses, the repository's own
address sent to other sites, a non-empty exclusion list."""
import os
import re
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = (".py", ".yml", ".yaml", ".md", ".txt", ".cfg", ".toml")
SKIP = {"requirements.txt", "seeds.txt", "domains.txt"}          # package hashes; employer seeds and domains (data)


def files():
    for base, dirs, names in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for n in names:
            if n.endswith(CODE) and n not in SKIP:
                p = os.path.join(base, n)
                yield os.path.relpath(p, HERE), open(p, encoding="utf-8", errors="replace").read()


class Public(unittest.TestCase):
    def test_no_list_of_digests(self):
        """A list of short digests is a list of guessable words: 1-3 word phrases fall to a dictionary whatever the salt."""
        for path, text in files():
            if path == os.path.join("tests", "test_public.py"):
                continue
            found = re.findall(r"""["'][0-9a-f]{16,64}["']""", text)
            self.assertLess(len(found), 3, f"{path}: {len(found)} quoted digests in one file")

    def test_no_personal_mail_address(self):
        """Code, docs and workflows carry no personal mailbox (the tests' fixtures quote postings, which name employers' own)."""
        personal = re.compile(r"@(gmail|googlemail|yahoo|outlook|hotmail|live|icloud|me|aol|proton|protonmail|pm)\.[a-z.]+$|\.edu$", re.I)
        for path, text in files():
            if path.startswith("tests" + os.sep):
                continue
            for addr in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text):
                self.assertIsNone(personal.search(addr), f"{path}: personal mail address")

    def test_requests_do_not_carry_this_repository(self):
        """Other sites' logs should not point back here: no repository address in a User-Agent or any request header."""
        for path, text in files():
            if path.endswith(".py") and not path.startswith("tests"):
                hit = re.search(r"""(?i)(user-agent|UA\s*=)[^\n]{0,200}github\.com/""", text)
                self.assertIsNone(hit, f"{path}: repository address in a User-Agent")

    def test_the_exclusion_list_is_empty(self):
        import discover as D
        if not os.environ.get("FEED_EXCLUDE"):
            self.assertEqual(len(D._Excluded.HASHES), 0)
            self.assertFalse(D.NEVER.search("any employer at all"))


if __name__ == "__main__":
    unittest.main()
