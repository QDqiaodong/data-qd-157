"""Revisable licence-observation warehouse.

The warehouse answers an operational question -- "how long was a component
observed in each licence state during a window?" -- never a legal one.  It
stores caller-supplied, versioned observations as immutable facts and keeps
materialized state intervals equivalent to the currently effective facts.

Version semantics (all business time comes from ``effective_at``, never from
file arrival order):

* A fact is identified by the immutable pair ``(event_id, revision)``.
* ``component_id`` is fixed for an ``event_id``; the caller always supplies it
  explicitly -- no lineage is inferred.
* Only the maximum revision of an event is effective.  When that revision is a
  ``retract`` the event contributes no state at all; older revisions can never
  resurrect.  Late smaller revisions are still persisted.
* Among effective ``put`` observations of one component sharing an
  ``effective_at``, the lexicographically greatest ``event_id`` wins; if it is
  later retracted the next-greatest event at that instant surfaces again.
* State runs from an observation's instant until the next observation; the
  time before the earliest observation is ``unknown``.  Intervals are
  half-open ``[a, b)``.

Storage design and the consistency duties it carries:

* ``observations`` is the append-only version fact table (primary key
  ``(event_id, revision)``).  It is the only table that keeps history; it is
  written once per accepted version and never updated or deleted.
* ``components`` is the persistent component directory.  A component that ever
  had an accepted record stays there even after every event is retracted, so
  an all-retracted component keeps reporting ``unknown`` instead of vanishing.
* ``intervals`` holds the current state timeline per component as disjoint,
  abutting half-open segments, the last one open-ended (``end_ts IS NULL``).
  Zero-length segments and adjacent equal-status segments are coalesced away,
  so every stored row represents real state seconds.  Window queries read only
  this table, the directory and the metadata row -- never the version history.

An import batch resolves versions, inserts facts, upserts the directory,
rebuilds intervals of *only the affected components* and bumps ``generation``
inside one ``BEGIN IMMEDIATE`` transaction, so facts and derived intervals can
never be seen in different generations.  A failure rolls the whole batch
back; there is no second commit for derived data.  Components untouched by a
batch are never traversed or rewritten.
"""
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3

from .common import ident
from .model import analyze

SCHEMA_VERSION = 1
OBSERVATION_LIMIT = 10000
WINDOW_COMPONENT_LIMIT = 1000
REVISION_MIN = 1
REVISION_MAX = 1_000_000_000
TIME_MIN = 0
TIME_MAX = 4102444800            # 2100-01-01T00:00:00Z
MAX_WINDOW_SECONDS = 366 * 86400
STATUSES = ('allowed', 'blocked', 'unknown')

# Test hook: invoked inside the import transaction after new facts and the
# component directory have been written but before any interval rebuild.
# Production code leaves it unset; tests use it to prove atomic rollback.
_FAULT_HOOK = None


def set_fault_hook(hook):
    """Install a fault hook (test support); pass ``None`` to remove it."""
    global _FAULT_HOOK
    _FAULT_HOOK = hook


class WarehouseError(ValueError):
    """Raised for warehouse contract violations; the CLI maps it to exit 2."""


def _bounded_int(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise WarehouseError('%s must be an integer in %d..%d'
                             % (label, minimum, maximum))
    return value


def _canonical_policy(policy):
    """Light shape validation; semantic validation is reused from analyze."""
    if not isinstance(policy, list) or len(policy) > 200:
        raise WarehouseError('policy must be an array of at most 200 entries')
    canonical = []
    for entry in policy:
        if not isinstance(entry, dict) or set(entry) != {
                'license', 'allowed', 'obligations'}:
            raise WarehouseError(
                'policy entry must contain exactly license, allowed, obligations')
        license_id = ident(entry['license'])
        if type(entry['allowed']) is not bool:
            raise WarehouseError('policy allowed must be a strict boolean')
        obligations = entry['obligations']
        if not isinstance(obligations, list) or len(obligations) > 50:
            raise WarehouseError('obligations must be an array of at most 50')
        canonical.append({
            'license': license_id,
            'allowed': entry['allowed'],
            'obligations': [ident(item) for item in obligations],
        })
    return canonical


def _normalize_rows(data):
    """Validate the observation document and return normalized row tuples.

    Each normalized row is a dict with a stable ``payload`` tuple used for
    idempotency and conflict detection:

    * put:     ``('put', effective_at, policy_json, expression)``
    * retract: ``('retract', None, None, None)``
    """
    if not isinstance(data, dict) or set(data) != {'observations'}:
        raise WarehouseError('root object must contain exactly observations')
    rows = data['observations']
    if not isinstance(rows, list) or len(rows) > OBSERVATION_LIMIT:
        raise WarehouseError(
            'observations must be an array of at most %d items'
            % OBSERVATION_LIMIT)

    normalized = []
    status_cache = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise WarehouseError('observation %d must be an object' % index)
        if 'action' not in row or not isinstance(row['action'], str):
            raise WarehouseError('observation %d: action is required' % index)
        action = row['action']
        event_id = ident(row.get('event_id'))
        revision = _bounded_int(row.get('revision'), REVISION_MIN,
                                REVISION_MAX, 'revision')
        component_id = ident(row.get('component_id'))
        if action == 'put':
            if set(row) != {'event_id', 'revision', 'component_id', 'action',
                            'effective_at', 'policy', 'expression'}:
                raise WarehouseError(
                    'put observation %d has unexpected or missing fields'
                    % index)
            effective_at = _bounded_int(row.get('effective_at'), TIME_MIN,
                                        TIME_MAX, 'effective_at')
            policy = _canonical_policy(row['policy'])
            expression = row['expression']
            if not isinstance(expression, str):
                raise WarehouseError('expression must be a string')
            policy_json = json.dumps(policy, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False)
            # The row state is the single-component judgement of the existing
            # analyze chain; callers can never supply a status themselves.
            cache_key = (policy_json, expression)
            status = status_cache.get(cache_key)
            if status is None:
                verdict = analyze({
                    'policy': row['policy'],
                    'components': [
                        {'id': component_id, 'expression': expression}],
                })
                status = verdict['components'][0]['status']
                status_cache[cache_key] = status
            payload = ('put', effective_at, policy_json, expression)
        elif action == 'retract':
            if set(row) != {'event_id', 'revision', 'component_id', 'action'}:
                raise WarehouseError(
                    'retract observation %d has unexpected or missing fields'
                    % index)
            effective_at = None
            policy_json = None
            expression = None
            status = None
            payload = ('retract', None, None, None)
        else:
            raise WarehouseError(
                'observation %d: action must be put or retract' % index)
        normalized.append({
            'event_id': event_id,
            'revision': revision,
            'component_id': component_id,
            'action': action,
            'effective_at': effective_at,
            'policy_json': policy_json,
            'expression': expression,
            'status': status,
            'payload': payload,
        })
    return normalized


def _validate_window_request(data):
    if not isinstance(data, dict) or set(data) != {
            'components', 'start', 'end'}:
        raise WarehouseError(
            'window document must contain exactly components, start, end')
    start = _bounded_int(data.get('start'), TIME_MIN, TIME_MAX, 'start')
    end = _bounded_int(data.get('end'), TIME_MIN, TIME_MAX, 'end')
    if not start < end:
        raise WarehouseError('require start < end')
    if end - start > MAX_WINDOW_SECONDS:
        raise WarehouseError('window must not exceed 366 days')
    raw_components = data['components']
    if not isinstance(raw_components, list) \
            or len(raw_components) > WINDOW_COMPONENT_LIMIT:
        raise WarehouseError(
            'components must be an array of at most %d ids'
            % WINDOW_COMPONENT_LIMIT)
    components = []
    seen = set()
    for value in raw_components:
        component_id = ident(value)
        if component_id in seen:
            raise WarehouseError('duplicate window component: ' + component_id)
        seen.add(component_id)
        components.append(component_id)
    return components, start, end


def _quote_uri(path):
    from urllib.parse import quote
    return quote(os.path.abspath(path), safe='/')


_SCHEMA = """
CREATE TABLE schema_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE components(
    component_id TEXT PRIMARY KEY
);
CREATE TABLE observations(
    event_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    component_id TEXT NOT NULL,
    action TEXT NOT NULL,
    effective_at INTEGER,
    policy_json TEXT,
    expression TEXT,
    status TEXT,
    PRIMARY KEY (event_id, revision)
) WITHOUT ROWID;
CREATE INDEX idx_observations_component
    ON observations(component_id);
CREATE TABLE intervals(
    component_id TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,
    status TEXT NOT NULL,
    PRIMARY KEY (component_id, start_ts)
) WITHOUT ROWID;
"""


def _reject_symlink(path):
    if os.path.islink(path):
        raise WarehouseError('warehouse path cannot be a symlink: %s' % path)


def open_warehouse(path, *, create, readonly=False):
    """Open the warehouse, creating an empty one when ``create`` is true.

    Queries pass ``create=False, readonly=True`` so a missing or
    foreign/corrupt file is reported instead of being created or overwritten,
    and the query connection can never take a write lock.
    """
    _reject_symlink(path)
    existed = os.path.exists(path)
    if existed and not os.path.isfile(path):
        raise WarehouseError('warehouse path is not a regular file: %s' % path)
    if not create and not existed:
        raise WarehouseError('warehouse does not exist: %s' % path)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        if readonly:
            # URI mode=ro guarantees no file is created and no write lock is
            # taken; immutable=1 is deliberately avoided because a concurrent
            # writer must remain visible after checkpoint.
            conn = sqlite3.connect(
                'file:%s?mode=ro' % _quote_uri(path), uri=True)
        else:
            conn = sqlite3.connect(str(path))
    except sqlite3.DatabaseError as error:
        raise WarehouseError('cannot open warehouse database: %s' % error)
    conn.isolation_level = None
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        # Single-operator tooling, but a reader and a writer can overlap under
        # WAL; wait briefly instead of failing on an instant lock contention.
        conn.execute('PRAGMA busy_timeout=5000')
        if not existed:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('BEGIN IMMEDIATE')
            try:
                # Individual executes (not executescript, which would force an
                # implicit commit) keep schema creation one transaction.
                for statement in (s.strip() for s in _SCHEMA.split(';')
                                  if s.strip()):
                    conn.execute(statement)
                conn.executemany(
                    'INSERT INTO schema_meta(key,value) VALUES(?,?)',
                    (('schema_version', str(SCHEMA_VERSION)),
                     ('generation', '0')))
                conn.execute('COMMIT')
            except Exception:
                conn.execute('ROLLBACK')
                raise
        else:
            # A non-SQLite or foreign file fails here and is never touched.
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                raise WarehouseError(
                    'file is not a license-planner warehouse: %s' % path)
            required = {'schema_meta', 'components', 'observations',
                        'intervals'}
            if not required <= tables:
                raise WarehouseError(
                    'unrecognized warehouse schema in %s' % path)
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None or int(row[0]) != SCHEMA_VERSION:
                raise WarehouseError(
                    'unsupported warehouse schema_version in %s' % path)
            if not readonly:
                conn.execute('PRAGMA journal_mode=WAL')
    except sqlite3.DatabaseError as error:
        conn.close()
        raise WarehouseError('warehouse database is unreadable: %s' % error)
    return conn


def _get_generation(conn):
    return int(conn.execute(
        "SELECT value FROM schema_meta WHERE key='generation'").fetchone()[0])


def _set_generation(conn, value):
    conn.execute("UPDATE schema_meta SET value=? WHERE key='generation'",
                 (str(value),))


def _rebuild_intervals(conn, component_id):
    """Replace the materialized timeline of one component from its facts.

    Only this component's version rows are read, so importing a batch never
    traverses another component's history or derived intervals.  The winner
    resolution mirrors the warehouse contract exactly: maximum revision per
    event, retract drops the whole event, greatest event id at a shared
    instant, half-open adjacency with zero-length/equal-status coalescing.
    """
    facts = conn.execute(
        'SELECT event_id, revision, action, effective_at, status '
        'FROM observations WHERE component_id=? '
        'ORDER BY event_id, revision', (component_id,)).fetchall()
    winners = {}
    for event_id, revision, action, effective_at, status in facts:
        current = winners.get(event_id)
        if current is None or revision > current[0]:
            winners[event_id] = (revision, action, effective_at, status)
    by_instant = {}
    for event_id, (revision, action, effective_at, status) in winners.items():
        if action != 'put':
            continue
        by_instant.setdefault(effective_at, []).append((event_id, status))
    points = sorted(by_instant)
    merged = []
    for instant in points:
        event_id, status = max(by_instant[instant], key=lambda item: item[0])
        if merged and merged[-1][1] == status:
            # Same status as the open segment: this observation's own segment
            # has zero duration, so the previous segment simply runs through.
            continue
        merged.append([instant, status])
    conn.execute('DELETE FROM intervals WHERE component_id=?', (component_id,))
    conn.executemany(
        'INSERT INTO intervals(component_id,start_ts,end_ts,status) '
        'VALUES(?,?,?,?)',
        [(component_id, start_ts,
          merged[index + 1][0] if index + 1 < len(merged) else None,
          status)
         for index, (start_ts, status) in enumerate(merged)])


def import_observations(conn, data, digest):
    """Validate and apply one observation batch on an open connection."""
    normalized = _normalize_rows(data)
    total_rows = len(normalized)

    # First pass over the batch itself: exact-key version payloads must agree
    # on repetition, and component_id is fixed per event_id inside the batch.
    batch_versions = {}
    batch_component = {}
    for row in normalized:
        key = (row['event_id'], row['revision'])
        previous = batch_versions.get(key)
        if previous is not None and previous != row['payload']:
            raise WarehouseError(
                'conflicting payloads for same version (%s,%d) in batch'
                % key)
        batch_versions.setdefault(key, row['payload'])
        owner = batch_component.get(row['event_id'])
        if owner is not None and owner != row['component_id']:
            raise WarehouseError(
                'component_id is fixed per event_id; conflict for %s'
                % row['event_id'])
        batch_component.setdefault(row['event_id'], row['component_id'])

    conn.execute('BEGIN IMMEDIATE')
    try:
        # Second pass against persisted facts: ownership and same-version
        # payload conflicts are detected even when the version is already
        # superseded by a larger revision.  Chunk the IN-list so a batch can
        # never exceed a build's bound-parameter limit.
        event_ids = list(batch_component)
        existing = {}
        for offset in range(0, len(event_ids), 900):
            chunk = event_ids[offset:offset + 900]
            placeholders = ','.join('?' for _ in chunk)
            for record in conn.execute(
                    'SELECT event_id, revision, component_id, action, '
                    'effective_at, policy_json, expression '
                    'FROM observations WHERE event_id IN (%s)'
                    % placeholders, chunk):
                eid, revision, owner, action, effective_at, policy_json, \
                    expression = record
                if owner != batch_component[eid]:
                    raise WarehouseError(
                        'component_id is fixed per event_id; conflict for %s'
                        % eid)
                payload = ((action, effective_at, policy_json, expression)
                           if action == 'put'
                           else ('retract', None, None, None))
                existing[(eid, revision)] = payload
        for key, payload in batch_versions.items():
            old_payload = existing.get(key)
            if old_payload is not None and old_payload != payload:
                raise WarehouseError(
                    'version (%s,%d) already exists with a different payload'
                    % key)

        inserted = 0
        new_rows = []
        row_by_key = {(row['event_id'], row['revision']): row
                      for row in normalized}
        for key, payload in batch_versions.items():
            if key in existing:
                continue
            event_id, revision = key
            row = row_by_key[key]
            new_rows.append((event_id, revision, row['component_id'],
                             row['action'], row['effective_at'],
                             row['policy_json'], row['expression'],
                             row['status']))
            inserted += 1
        conn.executemany(
            'INSERT INTO observations(event_id, revision, component_id, '
            'action, effective_at, policy_json, expression, status) '
            'VALUES(?,?,?,?,?,?,?,?)', new_rows)
        if inserted:
            affected = sorted(set(batch_component.values()))
            conn.executemany(
                'INSERT OR IGNORE INTO components(component_id) VALUES(?)',
                [(cid,) for cid in affected])

            if _FAULT_HOOK is not None:
                _FAULT_HOOK(conn, affected)

            # Only components with new facts can have changed timelines; a
            # pure duplicate batch leaves every derived row untouched.
            for component_id in affected:
                _rebuild_intervals(conn, component_id)
            generation = _get_generation(conn)
            _set_generation(conn, generation + 1)
            generation += 1
        else:
            generation = _get_generation(conn)
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise

    return {
        'schema_version': SCHEMA_VERSION,
        'source_sha256': digest,
        'generation': generation,
        'inserted': inserted,
        # Repeated rows inside this batch count as duplicates too.
        'duplicates': total_rows - inserted,
    }


def _day_bounds(start, end):
    """Yield ``(day_string, segment_start, segment_end)`` UTC day pieces.

    Each piece is the intersection of a UTC calendar day with [start, end);
    pieces stay inside the window, so their lengths sum to end - start.
    """
    current_date = datetime.fromtimestamp(start, timezone.utc).date()
    cursor = start
    while cursor < end:
        next_day = datetime.combine(
            current_date + timedelta(days=1),
            datetime.min.time(), timezone.utc).timestamp()
        piece_end = min(end, int(next_day))
        yield current_date.isoformat(), cursor, piece_end
        cursor = piece_end
        current_date = current_date + timedelta(days=1)


def _empty_buckets(start, end):
    return {day: [0, 0, 0] for day, _lo, _hi in _day_bounds(start, end)}


def window_report(conn, data, digest):
    """Build the read-only window report from persistent intervals only."""
    components, start, end = _validate_window_request(data)
    conn.execute('BEGIN')
    try:
        generation = _get_generation(conn)
        if components:
            known = set()
            for offset in range(0, len(components), 900):
                chunk = components[offset:offset + 900]
                placeholders = ','.join('?' for _ in chunk)
                known.update(row[0] for row in conn.execute(
                    'SELECT component_id FROM components '
                    'WHERE component_id IN (%s)' % placeholders, chunk))
            unknown_components = sorted(set(components) - known)
            if unknown_components:
                raise WarehouseError(
                    'unknown component id: %s' % unknown_components[0])

        window_length = end - start
        component_report = []
        day_totals = _empty_buckets(start, end)
        status_index = {name: index for index, name in enumerate(STATUSES)}

        for component_id in components:
            totals = [0, 0, 0]
            per_day = _empty_buckets(start, end)
            segments = conn.execute(
                'SELECT start_ts, end_ts, status FROM intervals '
                'WHERE component_id=? ORDER BY start_ts',
                (component_id,)).fetchall()
            for segment_start, segment_end, status in segments:
                clipped_start = max(start, segment_start)
                clipped_end = min(end, segment_end
                                  if segment_end is not None else end)
                if clipped_start >= clipped_end:
                    continue
                column = status_index[status]
                totals[column] += clipped_end - clipped_start
                for day, day_start, day_end in _day_bounds(
                        clipped_start, clipped_end):
                    per_day[day][column] += day_end - day_start
            known_seconds = totals[0] + totals[1]
            totals[2] = window_length - known_seconds
            # Unknown seconds fall in every day piece not covered by a
            # materialized state segment, including the pre-first-observation
            # region and gaps left by retractions.
            for day, day_start, day_end in _day_bounds(start, end):
                covered = sum(per_day[day])
                per_day[day][2] += (day_end - day_start) - covered
                for column in range(3):
                    day_totals[day][column] += per_day[day][column]
            component_report.append({
                'id': component_id,
                'allowed_seconds': totals[0],
                'blocked_seconds': totals[1],
                'unknown_seconds': totals[2],
            })

        days = [dict([('day', day)] + [
            ('%s_seconds' % name, day_totals[day][index])
            for index, name in enumerate(STATUSES)])
            for day in sorted(day_totals)]
        component_report.sort(key=lambda item: item['id'])
        report = {
            'schema_version': SCHEMA_VERSION,
            'source_sha256': digest,
            'generation': generation,
            'result': {
                'start': start,
                'end': end,
                'components': component_report,
                'days': days,
            },
        }
    except Exception:
        conn.execute('ROLLBACK')
        raise
    conn.execute('COMMIT')
    return report
