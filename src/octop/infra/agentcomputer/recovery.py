"""One bounded startup scan that fences unknown execution without replaying it.

The daemon owns the lifecycle mutex. This module neither proves process death
nor opens execution availability, stops a runtime, releases lanes, or calls an
engine. Installation identity and original acceptance facts remain immutable.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from uuid import UUID

from psycopg import Error as PostgresError

from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
)
from octop.infra.agentcomputer.dispatch import interrupt_startup_run
from octop.infra.agentcomputer.identity import IdentityError, read_identity
from octop.infra.db.services import SharedServices


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


def recover_dispatches(services: SharedServices) -> RecoveryReport:
    """Tighten only this box's persisted dispatches through one fixed high watermark.

    A failure rolls back its current Run and aborts startup; preceding complete
    per-Run commits are safe to observe on the next attempt. No missing identity
    or journal is reconstructed. A zero-candidate startup performs no file I/O.
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
        return RecoveryReport(
            scanned=scanned,
            tightened=tightened,
            unchanged=unchanged,
            effects_scanned=effects_scanned,
            effects_tightened=effects_tightened,
            effects_unchanged=effects_unchanged,
            pauses_scanned=pauses_scanned,
            pauses_expired=pauses_expired,
            pauses_unchanged=pauses_unchanged,
        )
    except StorageDataError:
        raise
    except (IdentityError, ValueError, TypeError, CommandAdmissionError) as exc:
        raise StorageDataError("invalid stored startup recovery fact") from exc
    except (sqlite3.DatabaseError, PostgresError, OSError) as exc:
        raise StorageUnavailableError("startup recovery storage unavailable") from exc
