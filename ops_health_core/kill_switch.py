"""Kill switch logic."""

import logging

from ops_health_core.model import HealthState, OpsPolicy, OpsSignal, OpsState
from ops_health_core.scorer import compute_health_score
from decision_schema.types import Action

logger = logging.getLogger(__name__)


def update_kill_switch(
    state: OpsState,
    policy: OpsPolicy,
    now_ms: int,
) -> OpsSignal:
    """
    Update kill switch state and produce signal.
    
    If state == RED => set cooldown_until = now + cooldown_ms
    During cooldown => deny_actions = True, recommended_action = HOLD
    On any exception: fail-closed (deny_actions=True, recommended_action=HOLD).
    
    Args:
        state: Current ops state
        policy: Ops policy
        now_ms: Current time (ms)
    
    Returns:
        OpsSignal with kill switch recommendations
    """
    # Prune timestamps in-place to avoid unbounded growth (F2 fix)
    from ops_health_core.windows import prune_timestamps_inplace
    
    prune_timestamps_inplace(state.error_timestamps, now_ms, policy.window_ms)
    prune_timestamps_inplace(state.rate_limit_timestamps, now_ms, policy.window_ms)
    prune_timestamps_inplace(state.reconnect_timestamps, now_ms, policy.window_ms)
    
    try:
        score, health_state = compute_health_score(state, policy, now_ms)
    except Exception as e:
        logger.warning("Kill switch fail-closed on exception: %s", type(e).__name__)
        return OpsSignal(
            score=0.0,
            state=HealthState.RED,
            deny_actions=True,
            cooldown_until_ms=None,
            recommended_action=Action.HOLD,
            reasons=["fail_closed_exception"],
        )

    # Check if already in cooldown
    in_cooldown = state.cooldown_until_ms is not None and now_ms < state.cooldown_until_ms

    # If RED state, activate kill switch
    if health_state == HealthState.RED and not in_cooldown:
        state.cooldown_until_ms = now_ms + policy.cooldown_ms

    # Update cooldown status
    if state.cooldown_until_ms is not None and now_ms >= state.cooldown_until_ms:
        # Cooldown expired
        state.cooldown_until_ms = None
        in_cooldown = False

    # Build reasons
    reasons = []
    if health_state == HealthState.RED:
        reasons.append("health_score_below_red_threshold")
    if in_cooldown:
        reasons.append("kill_switch_cooldown_active")

    return OpsSignal(
        score=score,
        state=health_state,
        deny_actions=in_cooldown or health_state == HealthState.RED,
        cooldown_until_ms=state.cooldown_until_ms,
        recommended_action=Action.HOLD if (in_cooldown or health_state == HealthState.RED) else Action.ACT,
        reasons=reasons,
    )
