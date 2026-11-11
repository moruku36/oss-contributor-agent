"""Three pure commit-policy cases; no command, model or network execution."""
from src.review.compliance import check_commit_message as check

POLICY = "conventional-commits"


def test_conventional_subjects_and_final_dco():
    assert check("fix(parser)!: reject invalid input", POLICY, False) == []
    assert check("custom: describe work\n\nBody.\n\n"
                 "Refs: #42\nSigned-off-by: A. Person <a+oss@example.org>\n",
                 POLICY, True) == []
    assert check("docs: clarify\n\nSigned-off-by: Person <p@example.org>\n"
                 "Reviewed-by: Reviewer <r@example.org>\n", POLICY, True) == []
    assert check("fix: line endings\r\n\r\n"
                 "Signed-off-by: Person <p@example.org>\r\n", POLICY, True) == []


def test_body_mentions_do_not_satisfy_dco():
    assert check("fix: change", POLICY, True)
    assert check("fix: change\n\nSigned-off-by: Person <p@example.org>\n\n"
                 "This is body text, not a final trailer.", POLICY, True)
    assert check("fix: change\n\nExample:\n"
                 "Signed-off-by: Person <p@example.org>", POLICY, True)
    assert check("fix: change\nSigned-off-by: Person <p@example.org>", POLICY, True)
    assert check("fix: change\n\nSigned-off-by: Person <not-an-email>", POLICY, True)
    assert check("fix: change\n\nSigned-off-by: <p@example.org>", POLICY, True)
    assert check("fix: change\n\nsigned-off-by: Person <p@example.org>", POLICY, True)


def test_unknown_policy_and_malformed_inputs_fail_closed():
    assert check("fix: change", "", False)
    assert check("fix: change", "read CONTRIBUTING.md", False)
    assert check("fix: change", POLICY, "false")
    assert check(None, POLICY, False)
    assert check("\ud800", POLICY, False)
    assert check("fix: " + "x" * 8192, POLICY, False)
    assert check("fix: " + "x" * 257, POLICY, False)
    assert check("fix: change\x00", POLICY, False)
    assert check("fix: change\rbody", POLICY, False)
    assert check("fix: change\nbody without separator", POLICY, False)
    assert check("plain subject", POLICY, False)
    assert check("Fix: change", POLICY, False)
    assert check("fix(scope with spaces): change", POLICY, False)
    assert check("fix: ", POLICY, False)
    assert check("fix: change ", POLICY, False)
