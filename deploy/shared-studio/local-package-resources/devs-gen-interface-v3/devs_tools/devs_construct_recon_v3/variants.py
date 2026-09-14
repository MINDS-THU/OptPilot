"""Locked recon variant profiles used by the factorial evaluation pilot."""

from __future__ import annotations


RECON_VARIANT_PROFILES: dict[str, dict[str, bool | int]] = {
    "recon_sr": {
        "enable_alignment_critic": False,
        "parent_use_raw_child_code": False,
        "summarize_after_generation": True,
        "rich_alignment_context": False,
    },
    "recon_critic": {
        "enable_alignment_critic": True,
        "parent_use_raw_child_code": False,
        "summarize_after_generation": True,
        "rich_alignment_context": False,
    },
    "recon_raw": {
        "enable_alignment_critic": False,
        "parent_use_raw_child_code": True,
        "summarize_after_generation": False,
        "rich_alignment_context": False,
    },
    "recon_align_raw": {
        "enable_alignment_critic": True,
        "parent_use_raw_child_code": True,
        "summarize_after_generation": False,
        "rich_alignment_context": False,
    },
    "recon_consensus_raw": {
        "enable_alignment_critic": False,
        "root_plan_draft_count": 1,
        # The historical variant name is retained for runner compatibility.
        # Coupled parents receive exact child plan contracts, not atomic source
        # bodies whose lifecycle code can be copied into the coupled class.
        "parent_use_raw_child_code": False,
        "summarize_after_generation": False,
        "rich_alignment_context": False,
    },
    "recon_consensus_repair2": {
        # Experimental and deliberately separate from recon_consensus_raw so
        # every behavior change can be rolled back by selecting the old name.
        "enable_alignment_critic": False,
        "root_plan_draft_count": 1,
        "parent_use_raw_child_code": False,
        "summarize_after_generation": False,
        "rich_alignment_context": False,
        "root_endpoint_audit_example": True,
        "continue_with_locked_interfaces": True,
        # One top-level run often exposes a short chain of independent defects
        # (for example: missing lifecycle method, then a stale coupling).  Three
        # bounded single-file repairs covers that common chain without turning
        # smoke repair into an open-ended debugging agent.
        "quick_smoke_max_repairs": 3,
    },
}


def get_recon_variant_profile(name: str) -> dict[str, bool | int]:
    """Return a copy so callers cannot mutate the locked profile table."""
    return dict(RECON_VARIANT_PROFILES.get(name, {}))
