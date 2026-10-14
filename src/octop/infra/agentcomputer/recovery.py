"""One bounded startup scan that fences unknown execution without replaying it.

The daemon owns the lifecycle mutex. This module neither proves process death
nor opens execution availability, stops a runtime, releases lanes, or calls an
engine. Installation identity and original acceptance facts remain immutable.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass
from uuid import UUID

from psycopg import Error as PostgresError

from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
)
from octop.infra.agentcomputer.dispatch import get_claim, interrupt_startup_run
from octop.infra.agentcomputer.identity import IdentityError, read_identity
from octop.infra.db.services import SharedServices
from octop.infra.history.store import probe_bound_turn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryReport:
    scanned: int = 0
    tightened: int = 0
    unchanged: int = 0
    effects_scanned: int = 0
    effects_tightened: int = 0
    effects_unchanged: int = 0
    pauses_scanned: int = 0
    pauses_expired: int = 0
    pauses_unchanged: int = 0
    history_observations: tuple[tuple[str, int], ...] = ()
    history_initial_messages: int = 0


def recover_dispatches(services: SharedServices) -> RecoveryReport:
    """Tighten only this box's persisted dispatches through one fixed high watermark.

    A failure before a Run's fence commit rolls it back. A later diagnostic
    failure cannot undo that committed fence and aborts startup; other complete
    per-Run commits remain safe. No missing identity or journal is reconstructed.
    A zero-candidate startup performs no file I/O.
    """
    try:
        with services.db.connect() as conn:
            through_pk = services.ac_run_repo.recovery_high_watermark(conn)
            effects_through_pk = services.ac_tool_effect_repo.recovery_high_watermark(conn)
            pauses_through_pk = services.ac_run_pause_repo.recovery_high_watermark(conn)
        if through_pk == 0 and effects_through_pk == 0 and pauses_through_pk == 0:
            return RecoveryReport()
        identity = read_identity(services.paths.agentcomputer_identity)
        after_pk = scanned = tightened = unchanged = 0
        history_observations: Counter[str] = Counter()
        history_initial_messages = 0
        while True:
            with services.db.connect() as conn:
                candidates = services.ac_run_repo.list_recovery_candidates(
                    conn, str(identity.boxID), after_pk=after_pk, through_pk=through_pk
                )
            if not candidates:
                break
            for candidate in candidates:
                if interrupt_startup_run(
                    services,
                    owner_id=candidate.owner_id,
                    run_id=UUID(candidate.run_id),
                    box_id=identity.boxID,
                ):
                    tightened += 1
                    # Re-read after the Run lock: an ACK may have arrived just
                    # before the fence. Never infer history from the candidate's
                    # earlier phase or from a different thread's latest Turn.
                    claim = get_claim(
                        services, owner_id=candidate.owner_id, run_id=UUID(candidate.run_id)
                    )
                    if claim is None:
                        raise StorageDataError("missing tightened dispatch claim")
                    if claim.engineReceipt is not None:
                        if services.config.database.is_sqlite:
                            intent = claim.intent
                            evidence = probe_bound_turn(
                                services.paths.root / "history_v2.sqlite",
                                identity=str(
                                    services.config.database.resolve_sqlite_path(
                                        services.paths.root
                                    ).resolve()
                                ),
                                turn_id=str(intent.turnID),
                                thread_id=intent.snapshot.octopThreadID,
                                agent_id=intent.snapshot.agentID,
                            )
                            history_observations[evidence.status] += 1
                            history_initial_messages += int(evidence.initial_message_saved)
                        else:
                            history_observations["archiveUnavailable"] += 1
                else:
                    unchanged += 1
                scanned += 1
                after_pk = candidate.pk
        from octop.infra.agentcomputer.effects import recover_effect

        effects_after_pk = effects_scanned = effects_tightened = effects_unchanged = 0
        while True:
            with services.db.connect() as conn:
                effects = services.ac_tool_effect_repo.list_recovery_candidates(
                    conn,
                    str(identity.boxID),
                    after_pk=effects_after_pk,
                    through_pk=effects_through_pk,
                )
            if not effects:
                break
            for effect in effects:
                if recover_effect(
                    services,
                    owner_id=effect.owner_id,
                    effect_id=UUID(effect.effect_id),
                    box_id=identity.boxID,
                ):
                    effects_tightened += 1
                else:
                    effects_unchanged += 1
                effects_scanned += 1
                effects_after_pk = effect.pk
        from octop.infra.agentcomputer.pauses import expire_pause

        pauses_after_pk = pauses_scanned = pauses_expired = pauses_unchanged = 0
        while True:
            with services.db.connect() as conn:
                pauses = services.ac_run_pause_repo.list_recovery_candidates(
                    conn,
                    str(identity.boxID),
                    after_pk=pauses_after_pk,
                    through_pk=pauses_through_pk,
                )
            if not pauses:
                break
            for pause in pauses:
                if expire_pause(
                    services,
                    owner_id=pause.owner_id,
                    pause_id=UUID(pause.pause_id),
                ):
                    pauses_expired += 1
                else:
                    pauses_unchanged += 1
                pauses_scanned += 1
                pauses_after_pk = pause.pk
        report = RecoveryReport(
            scanned=scanned,
            tightened=tightened,
            unchanged=unchanged,
            effects_scanned=effects_scanned,
            effects_tightened=effects_tightened,
            effects_unchanged=effects_unchanged,
            pauses_scanned=pauses_scanned,
            pauses_expired=pauses_expired,
            pauses_unchanged=pauses_unchanged,
            history_observations=tuple(sorted(history_observations.items())),
            history_initial_messages=history_initial_messages,
        )
        if report.history_observations:
            logger.warning(
                "AgentComputer startup history observations=%s initial_messages=%d",
                report.history_observations,
                report.history_initial_messages,
            )
        return report
    except StorageDataError:
        raise
    except (IdentityError, ValueError, TypeError, CommandAdmissionError) as exc:
        raise StorageDataError("invalid stored startup recovery fact") from exc
    except (sqlite3.DatabaseError, PostgresError, OSError) as exc:
        raise StorageUnavailableError("startup recovery storage unavailable") from exc
