import pytest

from raktsetu import compat


@pytest.mark.parametrize("recipient,donors", [
    ("O-", {"O-"}),
    ("O+", {"O+", "O-"}),
    ("A-", {"A-", "O-"}),
    ("A+", {"A+", "A-", "O+", "O-"}),
    ("B-", {"B-", "O-"}),
    ("B+", {"B+", "B-", "O+", "O-"}),
    ("AB-", {"AB-", "A-", "B-", "O-"}),
    ("AB+", set(compat.GROUPS)),
])
def test_compatible_donors_table(recipient, donors):
    assert set(compat.compatible_donors(recipient)) == donors
    assert compat.compatible_donors(recipient)[0] == recipient  # exact match first


def test_no_rh_negative_recipient_ever_gets_rh_positive_without_permission():
    for g in compat.GROUPS:
        if g.endswith("-"):
            assert all(d.endswith("-") for d in compat.compatible_donors(g))


def test_safe_substitutes_exclude_requested():
    assert compat.safe_substitutes("O-") == ()
    assert compat.safe_substitutes("A+") == ("A-", "O+", "O-")


@pytest.mark.parametrize("requested,expected", [
    ("O-", ("O+",)),
    ("A-", ("A+", "O+")),
    ("AB-", ("AB+", "A+", "B+", "O+")),
    ("O+", ()),
    ("AB+", ()),
])
def test_conditional_substitutes_are_rh_positive_only(requested, expected):
    assert compat.conditional_substitutes(requested) == expected


@pytest.mark.parametrize("text,group", [
    ("2 units O-negative, massive transfusion", "O-"),
    ("need O neg x2", "O-"),
    ("O-Ve 4 units", "O-"),
    ("AB+ for theatre", "AB+"),
    ("B positive, 3 units", "B+"),
    ("A -ve please", "A-"),
    ("O+ve restock", "O+"),
])
def test_parse_group(text, group):
    assert compat.parse_group(text) == group


@pytest.mark.parametrize("text", [
    "need blood asap pls",
    "send a negative crossmatch report",      # lower-case 'a' is not a blood group
    "O-negative or B positive, whichever",    # two groups: refuse to pick
])
def test_parse_group_refuses_ambiguity(text):
    assert compat.parse_group(text) is None


def test_from_eraktkosh_labels():
    assert compat.from_eraktkosh("O-Ve") == "O-"
    assert compat.from_eraktkosh("AB+Ve") == "AB+"
    assert compat.from_eraktkosh("garbage") is None
