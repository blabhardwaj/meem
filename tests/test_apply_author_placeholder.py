"""
apply_author_placeholder() only ever auto-filled the literal "[Author Name]"
/ "[Owner Name]" placeholders. A "Prepared by: [Test Lead Name]"-style
Sign-Off section (a different bracket wording, same "who wrote this"
meaning) was left unresolved. Fixed by matching on the "Prepared by:"
LABEL rather than the bracket's exact wording. Reviewer/Approver
placeholders must stay untouched — those are different people who haven't
actually reviewed/approved anything yet at draft time.
"""
from app.services.draft_workspace import apply_author_placeholder


def test_prepared_by_with_nonstandard_bracket_wording_resolves():
    content = "Prepared by: [Test Lead Name]"
    assert apply_author_placeholder(content, "Neo Mishra") == "Prepared by: Neo Mishra"


def test_reviewer_and_approver_placeholders_are_never_touched():
    content = (
        "Prepared by: [Test Lead Name]\n"
        "Reviewed by: [Reviewer Name]\n"
        "Approved by: [Approver Name]\n"
        "Date: [Date]"
    )
    result = apply_author_placeholder(content, "Neo Mishra")
    assert "Prepared by: Neo Mishra" in result
    assert "Reviewed by: [Reviewer Name]" in result
    assert "Approved by: [Approver Name]" in result
    assert "[Date]" not in result


def test_original_author_name_and_owner_name_still_work():
    assert apply_author_placeholder("Prepared by: [Author Name]", "Jane") == "Prepared by: Jane"
    assert apply_author_placeholder("Owner: [Owner Name]", "Jane") == "Owner: Jane"
    assert apply_author_placeholder("[Author Name]\n[Date]", "Jane").startswith("Jane\n")


def test_no_placeholder_is_a_no_op():
    content = "No placeholders here at all."
    assert apply_author_placeholder(content, "Jane") == content


def test_falsy_author_name_is_a_no_op():
    content = "Prepared by: [Test Lead Name]"
    assert apply_author_placeholder(content, None) == content
    assert apply_author_placeholder(content, "") == content
