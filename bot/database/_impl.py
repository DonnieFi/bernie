"""database._impl — thin re-export shim after Phase 1 domain split (8lx.1).

Prefer importing from database or database.<domain>. Kept so any residual
``from database._impl import X`` and WRITE_OPS introspection still resolve.
"""
from __future__ import annotations

from database.conn import *  # noqa: F403
from database.schema import *  # noqa: F403
from database.tasks import *  # noqa: F403
from database.cognitive import *  # noqa: F403
from database.identity import *  # noqa: F403
from database.usage import *  # noqa: F403
from database.activity import *  # noqa: F403
from database.meals import *  # noqa: F403
from database.presence import *  # noqa: F403
from database.notifications import *  # noqa: F403
from database.weather_cache import *  # noqa: F403
from database.drafts import *  # noqa: F403
from database.misc import *  # noqa: F403
from database.reminders_batch import *  # noqa: F403

# Explicit private re-exports (star-import skips _)
from database.conn import (  # noqa: F401
    _pkg,
    _resolve_db_path,
    _check_loop,
    _get_lock,
    _get_init_lock,
    _get_connection,
    _db_conn,
    _db_read,
    _log_lock_error,
)
from database.tasks import (  # noqa: F401
    _row_to_task,
    _row_to_automation,
    _system_task_row_from_cognitive,
    _reachable,
)
from database.schema import (  # noqa: F401
    _col_exists,
    _db_already_initialized,
    _table_exists,
    _legacy_tasks_fully_migrated,
    _set_last_vacuum_at,
)
from database.usage import (  # noqa: F401
    _load_price_index,
    _token_cost,
    _PRICE_EXACT,
    _PRICE_FRAGS,
)
from database.misc import (  # noqa: F401
    _person_id_for_discord,
)
from database.drafts import (  # noqa: F401
    _row_to_draft,
)

