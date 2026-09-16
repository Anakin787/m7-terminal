"""Who changed what, and when the system first noticed.

Nobody remembers to write an audit entry. So nothing here asks them to:
every run fingerprints the settings that decide what the engine may trade -
the universe, the strategy list, its parameters, the risk limits - compares
that against the fingerprint the last run stored, and records the difference.
A change made by hand in ``config.yaml`` shows up on the next run without the
person who made it doing anything at all.

Every entry carries two timestamps, because they are two different facts and
only one of them is ever certain.

``detected_at`` is when a process first saw the new value. Always known,
never wrong, and often much later than the edit.

``changed_at`` is when the setting itself last changed, recovered from the
file that holds it - and it can be absent. Where it comes from is recorded
in ``changed_by_method`` so a reader can weigh it:

* ``git`` - the setting lives in a tracked, unmodified source file, so the
  last commit that touched it gives both an exact time and a real author.
  This is the only case where "who" is a fact rather than an inference.
* ``mtime`` - the file's last write time. Right for the common case of one
  person editing config.yaml, but it is the last write to the *whole file*:
  edit the universe on Saturday and a Notion token on Sunday, and Sunday is
  what mtime reports. Restoring a backup or touching the file moves it too.
* ``None`` - the file could not be read at all. The entry still stands on
  ``detected_at``.

The actor follows the same grading. From ``git`` it is the commit author.
Otherwise it is the OS user of whichever process noticed - usually right on
a single-user machine, and never a security claim: this dashboard has no
authentication. An AI veto is written by the review that raised it, so that
one is exact.
"""

import getpass
import os
import socket
import subprocess
from datetime import datetime
from pathlib import Path

#: Settings whose change alters what the engine is allowed to do. Anything
#: outside this list (Notion token, news keywords, display names) is not
#: audited - not because it does not matter, but because an audit log that
#: logs everything gets skimmed instead of read.
CATEGORIES = ("universe", "strategies", "strategy_params", "limits")

ACTOR_HUMAN = "human"
ACTOR_AI = "ai"

#: Repository root, so the git lookups run in the right tree no matter where
#: the process was started from.
_ROOT = Path(__file__).resolve().parents[1]

#: Where each audited category physically lives. ``universe`` is decided at
#: run time - an empty ``trading.universe`` means the strategy falls back to
#: DEFAULT_UNIVERSE, which is code, not config.
CONFIG_FILE = "config.yaml"
UNIVERSE_MODULE = "src/strategy/universe.py"


def _text(value):
    return None if value is None else str(value)


def local_actor():
    """``user@host`` for whoever is running this process, best effort."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name is not a failure
        user = "unknown"
    try:
        host = socket.gethostname()
    except Exception:  # noqa: BLE001
        host = "unknown"
    return f"{user}@{host}"


def _git(args, timeout=5):
    """Run a git command in the repo, or return None if git cannot answer."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip()


def _git_origin(relative_path):
    """``(changed_at, author)`` from the last commit touching a clean file.

    Returns None when the file is untracked (config.yaml is, deliberately -
    it holds secrets) or when the working copy differs from the commit. A
    dirty file's last commit is emphatically not when it last changed, and
    reporting it would be the one kind of wrong this log must not be.
    """
    if _git(["ls-files", "--error-unmatch", "--", relative_path]) is None:
        return None
    if _git(["status", "--porcelain", "--", relative_path]):
        return None  # uncommitted local edit - the commit date is stale
    line = _git(["log", "-1", "--format=%aI%x09%an", "--", relative_path])
    if not line or "\t" not in line:
        return None
    changed_at, author = line.split("\t", 1)
    return changed_at, author


def change_origin(relative_path):
    """When ``relative_path`` last changed, and by whom, as best as is knowable.

    ``{"source", "changed_at", "changed_by_method", "actor"}``. ``changed_at``
    is None when the file cannot be read; the caller still has ``detected_at``.
    """
    origin = {
        "source": relative_path,
        "changed_at": None,
        "changed_by_method": None,
        "actor": local_actor(),
    }

    from_git = _git_origin(relative_path)
    if from_git is not None:
        origin["changed_at"], origin["actor"] = from_git
        origin["changed_by_method"] = "git"
        return origin

    try:
        mtime = os.path.getmtime(_ROOT / relative_path)
    except OSError:
        return origin
    origin["changed_at"] = datetime.fromtimestamp(mtime).isoformat()
    origin["changed_by_method"] = "mtime"
    return origin


def source_for(category, trading_config):
    """Which file holds the setting behind ``category``.

    The universe is the interesting one: an empty ``trading.universe`` means
    the strategy uses DEFAULT_UNIVERSE, so the change came from source code
    (which git can date exactly), not from config.yaml (which it cannot).
    """
    if category == "universe" and not (getattr(trading_config, "universe", None) or []):
        return UNIVERSE_MODULE
    return CONFIG_FILE


def fingerprint(trading_config, universe):
    """The audited settings, flattened to plain strings.

    Strings throughout so that a Decimal weight, an int read from YAML and
    the same value read back out of Firestore all compare equal - a diff that
    fires because ``0.35`` came back as ``Decimal('0.35')`` would train the
    reader to ignore this log.
    """
    instruments = {}
    for instrument in getattr(universe, "instruments", ()) or ():
        instruments[instrument.symbol] = {
            "kind": _text(instrument.kind),
            "max_weight": _text(instrument.max_weight),
            "leverage": _text(instrument.leverage),
            "enabled": _text(instrument.enabled),
        }

    return {
        "universe": instruments,
        "strategies": [_text(s) for s in getattr(trading_config, "strategies", []) or []],
        "strategy_params": {
            key: _text(value)
            for key, value in (getattr(trading_config, "strategy_params", {}) or {}).items()
        },
        "limits": {
            key: _text(value)
            for key, value in (getattr(trading_config, "limits", {}) or {}).items()
        },
    }


def _diff_mapping(before, after):
    """``[{target, before, after}]`` for two flat-ish mappings."""
    changes = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        changes.append({"target": key, "before": old, "after": new})
    return changes


def _diff_universe(before, after):
    changes = []
    for symbol in sorted(set(before) | set(after)):
        old, new = before.get(symbol), after.get(symbol)
        if old == new:
            continue
        if old is None:
            changes.append({"target": symbol, "before": None, "after": "추가"})
        elif new is None:
            changes.append({"target": symbol, "before": "보유", "after": "제거"})
        else:
            for field in sorted(set(old) | set(new)):
                if old.get(field) != new.get(field):
                    changes.append(
                        {
                            "target": f"{symbol}.{field}",
                            "before": old.get(field),
                            "after": new.get(field),
                        }
                    )
    return changes


def diff(before, after):
    """``{category: [changes]}`` for the categories that actually moved.

    An empty dict means nothing audited changed, which is the normal result
    of nearly every run.
    """
    before = before or {}
    result = {}
    for category in CATEGORIES:
        old = before.get(category)
        new = after.get(category)
        if category == "universe":
            changes = _diff_universe(old or {}, new or {})
        elif category == "strategies":
            changes = (
                []
                if (old or []) == (new or [])
                else [{"target": "strategies", "before": old, "after": new}]
            )
        else:
            changes = _diff_mapping(old or {}, new or {})
        # A first-ever run has no stored fingerprint at all. Recording the
        # whole universe as "added" on that run would be technically true and
        # completely useless, so no diff is produced - `record_config_changes`
        # writes one `baseline_entry` instead.
        if changes and before:
            result[category] = changes
    return result


def entries_from_diff(changes_by_category, trading_config=None, actor=None, detected_at=None):
    """Turn a diff into audit entries, one per category.

    Each category is dated from the file that actually holds it, so a
    universe living in code gets a git commit time and author while a
    config.yaml edit gets an mtime. ``actor``, when passed, overrides both -
    used by tests, not by the running system.
    """
    detected_at = detected_at or datetime.now().isoformat()
    entries = []
    for category, changes in changes_by_category.items():
        origin = change_origin(source_for(category, trading_config))
        entries.append(
            {
                "detected_at": detected_at,
                "changed_at": origin["changed_at"],
                "changed_by_method": origin["changed_by_method"],
                "actor_kind": ACTOR_HUMAN,
                "actor": actor or origin["actor"],
                "source": origin["source"],
                "category": category,
                "summary": _summarize(category, changes),
                "changes": changes,
            }
        )
    return entries


def _summarize(category, changes):
    if category == "universe":
        added = [c["target"] for c in changes if c.get("after") == "추가"]
        removed = [c["target"] for c in changes if c.get("after") == "제거"]
        edited = [c["target"] for c in changes if c.get("after") not in ("추가", "제거")]
        parts = []
        if added:
            parts.append(f"추가 {len(added)}종목 ({', '.join(added[:5])}{'…' if len(added) > 5 else ''})")
        if removed:
            parts.append(f"제거 {len(removed)}종목 ({', '.join(removed[:5])}{'…' if len(removed) > 5 else ''})")
        if edited:
            parts.append(f"설정 변경 {len(edited)}건")
        return " · ".join(parts)
    return f"{len(changes)}개 항목 변경: " + ", ".join(c["target"] for c in changes[:5])


def review_entries(review, actor="AI universe review", detected_at=None):
    """Audit entries for what an AI universe review did and proposed.

    Vetoes and candidates are recorded as separate entries because they are
    separate kinds of fact: one changed what the engine will do, the other is
    a suggestion nobody has acted on. Flattening them into one row would
    reproduce, in the audit log, exactly the confusion the report layout
    works to avoid.
    """
    detected_at = detected_at or datetime.now().isoformat()
    entries = []
    vetoes = getattr(review, "vetoes", ()) or ()
    candidates = getattr(review, "candidates", ()) or ()

    if vetoes:
        entries.append(
            {
                "detected_at": detected_at,
                # The review raised it just now, so there is no gap between
                # the change and its detection - this is the one actor whose
                # timing needs no recovering.
                "changed_at": detected_at,
                "changed_by_method": "direct",
                "actor_kind": ACTOR_AI,
                "actor": actor,
                "source": "universe_review",
                "category": "veto",
                "summary": "신규 매수 보류: " + ", ".join(v.symbol for v in vetoes),
                "changes": [
                    {
                        "target": veto.symbol,
                        "before": None,
                        "after": f"[{veto.category}] {veto.reason}",
                        "evidence": veto.evidence,
                    }
                    for veto in vetoes
                ],
            }
        )
    if candidates:
        entries.append(
            {
                "detected_at": detected_at,
                "changed_at": detected_at,
                "changed_by_method": "direct",
                "actor_kind": ACTOR_AI,
                "actor": actor,
                "source": "universe_review",
                "category": "candidate",
                "summary": "편입 후보 제안(미반영): "
                + ", ".join(c.symbol for c in candidates),
                "changes": [
                    {"target": c.symbol, "before": None, "after": c.reason}
                    for c in candidates
                ],
            }
        )
    return entries


def veto_cleared_entry(symbol, reason=None, actor=None, detected_at=None):
    """One audit row for a human lifting an AI veto.

    Audited for the same reason the kill switch is: a veto blocks buys the
    strategy wanted to make, so lifting one changes what the engine may do
    next. The act is the write, so ``changed_at`` equals ``detected_at``.
    """
    detected_at = detected_at or datetime.now().isoformat()
    return {
        "detected_at": detected_at,
        "changed_at": detected_at,
        "changed_by_method": "direct",
        "actor_kind": ACTOR_HUMAN,
        "actor": actor or local_actor(),
        "source": "dashboard",
        "category": "veto",
        "summary": f"{symbol} 매수 보류 해제" + (f" (사유: {reason})" if reason else ""),
        "changes": [{"target": symbol, "before": "보류", "after": "해제"}],
    }


def kill_switch_entry(state, active, actor=None, detected_at=None):
    """One audit row for a kill-switch flip, written by whoever flipped it.

    This is the one audited change with no file to date afterwards: the act
    *is* the write, so ``changed_at`` equals ``detected_at`` and the method is
    ``direct``. Only a real transition should be recorded - re-engaging an
    already-engaged switch changes nothing and deserves no row, or the log
    fills with non-events on the day it matters most.
    """
    detected_at = detected_at or datetime.now().isoformat()
    reason = (state or {}).get("reason")
    return {
        "detected_at": detected_at,
        "changed_at": detected_at,
        "changed_by_method": "direct",
        "actor_kind": ACTOR_HUMAN,
        "actor": actor or local_actor(),
        "source": (state or {}).get("path") or "KILL_SWITCH",
        "category": "kill_switch",
        "summary": (
            "킬 스위치 발동 — 모든 발주 중단" + (f": {reason}" if reason else "")
            if active
            else "킬 스위치 해제 — 발주 재개"
        ),
        "changes": [
            {
                "target": "kill_switch",
                "before": "해제" if active else "발동",
                "after": "발동" if active else "해제",
                "evidence": reason,
            }
        ],
    }


def baseline_entry(settings, trading_config=None, actor=None, detected_at=None):
    """The single entry a first-ever run writes instead of 39 "added" rows.

    Without it the page is empty until somebody happens to change something,
    which reads like the log is broken rather than like nothing has changed
    yet. One row saying what the starting point was fixes that and claims
    nothing untrue: this really is the first state the system ever saw.
    """
    universe = settings.get("universe") or {}
    strategies = settings.get("strategies") or []
    origin = change_origin(source_for("universe", trading_config))
    return {
        "detected_at": detected_at or datetime.now().isoformat(),
        "changed_at": origin["changed_at"],
        "changed_by_method": origin["changed_by_method"],
        "actor_kind": ACTOR_HUMAN,
        "actor": actor or origin["actor"],
        "source": origin["source"],
        "category": "baseline",
        "summary": (
            f"감사 로그 시작 — 유니버스 {len(universe)}종목 · 전략 {len(strategies)}개 "
            "· 이후 변경분만 기록됩니다"
        ),
        "changes": [
            {"target": "universe", "before": None, "after": f"{len(universe)}종목"},
            {"target": "strategies", "before": None, "after": ", ".join(strategies) or "—"},
        ],
    }


def record_config_changes(store, trading_config, universe, actor=None):
    """Compare settings against the stored fingerprint and log what moved.

    Returns the entries written (empty on an unchanged run). Never raises:
    an audit log that can stop the daily report from running would be traded
    away for the report on the first failure, and then there would be neither.
    """
    try:
        current = fingerprint(trading_config, universe)
        previous = store.audit_fingerprint()
        if previous is None:
            entries = [baseline_entry(current, trading_config, actor=actor)]
        else:
            entries = entries_from_diff(
                diff(previous, current), trading_config, actor=actor
            )
        if entries:
            store.save_audit_entries(entries)
        if previous != current:
            store.save_audit_fingerprint(current)
        return entries
    except Exception as exc:  # noqa: BLE001 - never fatal
        print(f"경고: 감사 로그를 기록하지 못했습니다 ({exc})")
        return []
