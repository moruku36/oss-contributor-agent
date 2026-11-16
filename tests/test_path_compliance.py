"""Offline tests for declared-path scope checks (stdlib unittest only)."""

import unittest

from src.review.path_compliance import (
    DUPLICATE_PATH,
    INVALID_COLLECTION,
    INVALID_PATH,
    LIMIT_EXCEEDED,
    OUT_OF_SCOPE,
    PROTECTED_PATH,
    check_changed_paths as check,
)

BS = chr(92)


class DeclaredPathTests(unittest.TestCase):
    def test_P01_accepts_exact_unicode_nested_paths(self):
        changed = ["src/日本語/ü.py", "docs/a b.md"]
        self.assertEqual(check(changed, ["docs/a b.md", "src/日本語/ü.py", "x"]), [])

    def test_P02_scope_is_exact_and_globs_do_not_expand(self):
        self.assertEqual(check(["src/a.py", "src/b.py"], ["src/a.py"]), [OUT_OF_SCOPE])
        self.assertEqual(check(["src/a.py"], ["src/*.py"]), [OUT_OF_SCOPE])
        self.assertEqual(check(["src/*.py"], ["src/a.py"]), [OUT_OF_SCOPE])
        self.assertEqual(check(["SRC/a.py"], ["src/a.py"]), [OUT_OF_SCOPE])
        self.assertEqual(check(["a"], []), [OUT_OF_SCOPE])

    def test_P03_ci_and_vcs_protection_overrides_allow(self):
        for p in [".github/workflows/ci.yml", ".GitHub/x", ".git/config", "a/.GIT/x",
                  ".gitlab/ci.yml", ".gitlab", ".circleci/config.yml",
                  ".buildkite/p.yml", ".gitlab-ci.yml", "azure-pipelines.yml",
                  "Azure-Pipelines.yml"]:
            self.assertEqual(check([p], [p]), [PROTECTED_PATH], p)
        for p in ["src/.github/x", "docs/azure-pipelines.yml", ".gitignore", "a.github"]:
            self.assertEqual(check([p], [p]), [], p)
        self.assertEqual(check([".git/x", "z"], [".git/x"]), [OUT_OF_SCOPE, PROTECTED_PATH])

    def test_P04_license_basename_case_and_suffix_override(self):
        for p in ["LICENSE", "license.md", "docs/Licence.txt", "COPYING.LIB", "NOTICE",
                  "pkg/notice.txt", "COPYRIGHT", "AUTHORS.rst", "a/b/Authors"]:
            self.assertEqual(check([p], [p]), [PROTECTED_PATH], p)
        for p in ["NOTICES", "authors_list.py", "LICENSES/x.txt", "my.license/x"]:
            self.assertEqual(check([p], [p]), [], p)

    def test_P05_malformed_paths_and_unicode_controls(self):
        bad = ["/abs", "a//b", "a/./b", "a/../b", "a/", "a" + BS + "b", "C:/x", "a:b", "", ".",
               "..", "a" + chr(0) + "b", "a" + chr(0x7f) + "b", "a" + chr(10) + "b",
               "a" + chr(0x85) + "b", "a" + chr(0x202e) + "b", "a" + chr(0xd800) + "b",
               "a" + chr(0x2028) + "b"]
        for p in bad:
            self.assertEqual(check([p], ["ok"]), [INVALID_PATH], repr(p))
            self.assertEqual(check(["ok"], [p]), [INVALID_PATH], repr(p))
        self.assertEqual(check(["ok", "/abs"], ["ok"]), [INVALID_PATH])

    def test_P06_collection_types_duplicates_and_emptiness(self):
        class S(str):
            pass

        for changed, allowed in [(("a",), ["a"]), (["a"], ("a",)), (None, []), ("a", ["a"]),
                                 (["a"], None), ([], ["a"]), ([], [])]:
            self.assertEqual(check(changed, allowed), [INVALID_COLLECTION])
        for changed, allowed in [([1], []), ([b"a"], []), ([S("a")], ["a"]), (["a"], [None])]:
            self.assertEqual(check(changed, allowed), [INVALID_PATH])
        self.assertEqual(check(["a", "a"], ["a"]), [DUPLICATE_PATH])
        self.assertEqual(check(["a"], ["a", "a"]), [DUPLICATE_PATH])
        self.assertEqual(check(["a"], []), [OUT_OF_SCOPE])

    def test_P07_count_byte_and_aggregate_bounds(self):
        many = ["p%d" % i for i in range(128)]
        self.assertEqual(check(many, many), [])
        self.assertEqual(check(many + ["extra"], many), [LIMIT_EXCEEDED])
        self.assertEqual(check(["a"], many + ["extra"]), [LIMIT_EXCEEDED])
        self.assertEqual(check(["a" * 1024], ["a" * 1024]), [])
        self.assertEqual(check(["a" * 1025], ["a"]), [LIMIT_EXCEEDED])
        self.assertEqual(check(["é" * 512], ["é" * 512]), [])
        self.assertEqual(check(["é" * 512 + "a"], ["a"]), [LIMIT_EXCEEDED])
        big = ["%04d" % i + "a" * 1020 for i in range(16)]
        self.assertEqual(check(big, big), [])
        more = ["%04d" % i + "a" * 1020 for i in range(17)]
        self.assertEqual(check(more, more), [LIMIT_EXCEEDED])

    def test_P08_pure_deterministic_and_sanitized(self):
        changed = [".git/a", ".git/b", "secret-token/x", "z"]
        allowed = ["z"]
        before = (list(changed), list(allowed))
        first = check(changed, allowed)
        second = check(changed, allowed)
        self.assertEqual((changed, allowed), before)
        self.assertEqual(first, [OUT_OF_SCOPE, PROTECTED_PATH])
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        bad = check(["secret-token/../x"], ["ok"])
        self.assertEqual(bad, [INVALID_PATH])
        self.assertNotIn("secret", str(first) + str(bad))


if __name__ == "__main__":
    unittest.main()
