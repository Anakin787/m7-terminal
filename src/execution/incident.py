"""Holding LIVE trading after something nobody anticipated, and saying why.

A LIVE run that hits an error it does not understand has two bad options
left to it - carry on, or retry - and both can place a real order on a
misread state. So it does neither: it engages the kill switch, which every
later run and the risk gate already honour, and writes the reason to the
audit log where the dashboard shows it the next morning. Trading resumes
only when a person has read that entry and released the switch.

"Unknown" is deliberately broad. Known, handled conditions - the market is
closed, the gate rejected a signal, a broker code ``TERMINAL_CODES`` names -
do not hold anything. Everything else does: an exception that escaped the
run, an order whose response was lost, an error code nobody has seen, a
fill the reconciler cannot read. Holding on a false alarm costs a day of
DCA; not holding on a real one can cost a duplicated position.

Every step is best-effort and ordered like the dashboard's own PAUSE: the
switch first, then the log. A hold nobody logged beats a log of a hold
that did not happen.
"""

import sys
import traceback
from datetime import datetime

from src.audit import kill_switch_entry
from src.execution.risk import engage_kill_switch, kill_switch_state

CATEGORY = "incident"
ACTOR_SYSTEM = "system"

#: What the reader should do, attached to every entry so the log says it
#: where it is read rather than in a runbook nobody opens at 8am.
NEXT_STEP = "원인 확인 후 대시보드에서 킬 스위치를 해제하면 다음 실행부터 매매가 재개됩니다."

#: Lines of traceback kept in an entry - the tail, where the cause is.
_TRACEBACK_LINES = 12


def describe_exception(exc):
    """``Type: message`` plus the traceback's tail, for an entry's evidence."""
    tail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    tail = "\n".join(tail.strip().splitlines()[-_TRACEBACK_LINES:])
    return f"{type(exc).__name__}: {exc}\n{tail}"


def incident_entry(summary, findings, source, actor, detected_at=None):
    """One audit row for a hold. ``findings`` is ``[(target, evidence), ...]``."""
    detected_at = detected_at or datetime.now().isoformat()
    return {
        "detected_at": detected_at,
        "changed_at": detected_at,
        "changed_by_method": "direct",
        "actor_kind": ACTOR_SYSTEM,
        "actor": actor,
        "source": source,
        "category": CATEGORY,
        "summary": f"LIVE 매매 보류 — {summary}",
        "changes": [
            {"target": target, "before": "", "after": "보류", "evidence": evidence}
            for target, evidence in findings
        ]
        + [{"target": "조치", "before": "", "after": "", "evidence": NEXT_STEP}],
    }


def hold_trading(store, kill_switch_path, summary, findings, source, actor, clock=None):
    """Engage the kill switch and audit why. Returns True if the switch is on.

    Never raises: this runs on the way out of a failure, and a second
    failure here must not hide the first. What could not be done is printed
    to stderr, which is the one channel a Cloud Run job always has.
    """
    now = (clock or datetime.now)()
    detected_at = now.isoformat()
    reason = f"자동 보류: {summary}"

    was_active = False
    try:
        was_active = kill_switch_state(kill_switch_path, store=store)["active"]
    except Exception:  # noqa: BLE001 - engage regardless
        pass

    state = None
    try:
        state = engage_kill_switch(
            kill_switch_path, reason=reason, actor=actor, at=now, store=store
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"!!! 킬 스위치 발동 실패: {exc}", file=sys.stderr)

    entries = [incident_entry(summary, findings, source, actor, detected_at)]
    if state is not None and state["active"] and not was_active:
        switch = kill_switch_entry(state, True, actor=actor, detected_at=detected_at)
        switch["actor_kind"] = ACTOR_SYSTEM
        entries.append(switch)
    try:
        if store is not None:
            store.save_audit_entries(entries)
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"!!! 감사 로그 기록 실패: {exc}", file=sys.stderr)

    print(f"!!! LIVE 매매를 보류했습니다 (킬 스위치 발동): {summary}", file=sys.stderr)
    for target, evidence in findings:
        print(f"    {target}: {evidence.splitlines()[0] if evidence else ''}", file=sys.stderr)
    print(f"    {NEXT_STEP}", file=sys.stderr)
    return bool(state and state["active"])
