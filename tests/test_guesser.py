"""Offline unit tests for name parsing and pattern learning (no network)."""

from email_guesser import (
    build_candidates,
    distributions_from_pairs,
    infer_from_shape,
    names_on_page,
    normalize_candidates,
    rank_candidates,
    render,
    split_name,
)


def test_split_name_handles_accents_and_single_names():
    assert split_name("José María O'Brien-Smith") == ("jose", "obrien-smith")  # hyphen kept
    assert split_name("Jane Doe") == ("jane", "doe")
    assert split_name("Madonna") == ("madonna", "")


def test_hyphenated_surname_matches_both_forms():
    from email_guesser import rendered_locals
    locals_ = rendered_locals("{first}.{last}", "gus", "garcia-roberts")
    assert "gus.garcia-roberts" in locals_   # hyphen kept
    assert "gus.garciaroberts" in locals_    # hyphen dropped
    # a guess for this person must include the real (hyphenated) address
    cands = normalize_candidates(build_candidates("gus", "garcia-roberts", "x.org",
                                                  {"{first}.{last}": 1.0}))
    assert "gus.garcia-roberts@x.org" in {c["email"] for c in cands}


def test_render_patterns():
    assert render("{first}.{last}", "jane", "doe") == "jane.doe"
    assert render("{f}{last}", "jane", "doe") == "jdoe"
    assert render("{last}", "jane", "") is None   # no last name -> no last-name pattern


def test_names_on_page_skips_titles_and_middle_initials():
    html = "<p>Director Jane A. Doe and Senior Engineer Rahul Sharma</p>"
    pairs = names_on_page(html)
    assert ("jane", "doe") in pairs          # middle initial skipped
    assert ("rahul", "sharma") in pairs
    assert ("director", "jane") not in pairs  # title word dropped


def test_infer_from_shape():
    assert infer_from_shape("rahul.sharma") == "{first}.{last}"
    assert infer_from_shape("a.sharma") == "{f}.{last}"
    assert infer_from_shape("plainname") is None


def test_candidates_rank_learned_pattern_first():
    # a company that clearly uses {f}{last}
    shares = {"{f}{last}": 0.9, "{first}.{last}": 0.1}
    cands = normalize_candidates(build_candidates("jane", "doe", "acme.com", shares))
    assert cands[0]["email"] == "jdoe@acme.com"
    assert cands[0]["confidence"] > cands[1]["confidence"]


def test_confidences_are_normalised():
    cands = normalize_candidates(build_candidates("jane", "doe", "acme.com", {}))
    assert abs(sum(c["confidence"] for c in cands) - 1.0) < 0.05
    assert all(0 <= c["confidence"] <= 0.99 for c in cands)


def test_rank_never_proposes_a_taken_mailbox():
    # jdoe@ is held by someone else -> must not be the guess for a different Jane Doe
    cands = rank_candidates("jane", "doe", "acme.com", {"{f}{last}": 1.0}, occupied={"jdoe"})
    assert all(c["email"] != "jdoe@acme.com" for c in cands)


def test_rank_switches_to_fallback_on_collision():
    # org uses {first}@ normally, {f}{last}@ when the first name is taken
    shares = {"{first}": 1.0}
    fallback = {"{f}{last}": 1.0}
    # 'aaron' is already taken -> Aaron Mackey should get the fallback form
    cands = rank_candidates("aaron", "mackey", "eff.org", shares,
                            occupied={"aaron"}, fallback_shares=fallback)
    assert cands[0]["email"] == "amackey@eff.org"
    # with no collision, the primary pattern still wins
    cands2 = rank_candidates("adam", "schwartz", "eff.org", shares,
                             occupied={"aaron"}, fallback_shares=fallback)
    assert cands2[0]["email"] == "adam@eff.org"


def test_distributions_learn_primary_and_fallback():
    # three people: two use {first}, one (whose first name collides) uses {f}{last}
    pairs = [("adam", "schwartz", "adam"), ("aaron", "jue", "aaron"),
             ("aaron", "mackey", "amackey")]
    shares, fallback, occupied = distributions_from_pairs(pairs)
    assert max(shares, key=shares.get) == "{first}"
    assert "{f}{last}" in fallback          # the non-primary person defines the fallback
    assert {"adam", "aaron", "amackey"} == occupied
