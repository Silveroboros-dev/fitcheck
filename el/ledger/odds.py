"""Pure, deterministic ledger helpers (step 6): thesis-side odds orientation
and the strict-save rule. No I/O, no model calls — unit-testable in isolation.
"""

ODDS_SIDE_YES = "yes"
ODDS_SIDE_NO = "no"
ODDS_SIDE_UNKNOWN = "side_unknown"


def orient_odds(
    current_probability: float | None, thesis_side: str | None
) -> tuple[float | None, str]:
    """Frozen market YES probability -> thesis-side odds-at-entry + side.

    The candidate member stores the market YES probability; the ledger records
    the odds from the THESIS's side (P1 thesis_side):

      yes              -> (p,       "yes")
      no               -> (1 - p,   "no")
      unknown / None    -> (None,    "side_unknown")   # NEVER the raw YES here
      missing price (p None) -> (None, side)            # side known, price gone

    Raw YES already lives on candidate_set_members.current_probability; an
    unresolved side must not smuggle it into odds_at_entry (review amendment).
    """
    side = (
        thesis_side
        if thesis_side in (ODDS_SIDE_YES, ODDS_SIDE_NO)
        else ODDS_SIDE_UNKNOWN
    )
    if side == ODDS_SIDE_UNKNOWN or current_probability is None:
        return None, side
    if side == ODDS_SIDE_YES:
        return current_probability, ODDS_SIDE_YES
    return 1.0 - current_probability, ODDS_SIDE_NO


def strict_save_violations(
    *,
    conviction_level,
    intended_exposure_bucket,
    user_justification: str | None,
    requires_blind_prior: bool,
    blind_prior_present: bool,
) -> list[str]:
    """Strict-save rule (H2): conviction_level + intended_exposure_bucket +
    user_justification all required; the agent surface additionally requires a
    prior blind prior (the lock). Returns violations — empty means save may
    proceed. Progressive save (relaxing these) is a deferred A/B."""
    violations: list[str] = []
    if conviction_level is None:
        violations.append("conviction_level is required (strict save)")
    if intended_exposure_bucket is None:
        violations.append("intended_exposure_bucket is required (strict save)")
    if not (user_justification and user_justification.strip()):
        violations.append("user_justification is required (strict save)")
    if requires_blind_prior and not blind_prior_present:
        violations.append(
            "agent surface requires a blind prior before save (no lock found)"
        )
    return violations
