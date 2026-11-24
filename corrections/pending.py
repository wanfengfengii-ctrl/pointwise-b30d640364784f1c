from archive_db import ConflictError

from layout.values import dump_reading


class FieldsBusy(ConflictError):
    def __init__(self, address, field_name, holder):
        self.address = address
        self.field_name = field_name
        self.holder = holder
        super().__init__(f'{address.row_id}/{field_name} is pending with {holder}')


def initialise_reservations(db):
    # A field may carry several holders: only explicit same-value shared tickets
    # are allowed to co-hold one (volume, row, field).
    db.execute('''CREATE TABLE IF NOT EXISTS field_reservations (
        volume_id TEXT NOT NULL REFERENCES originals(volume_id),
        row_id TEXT NOT NULL,
        field_name TEXT NOT NULL,
        holder TEXT NOT NULL REFERENCES correction_requests(request_id),
        PRIMARY KEY (volume_id, row_id, field_name, holder)
    )''')
    # Upgrade an archive created with the original single-holder primary key.
    columns = db.execute('PRAGMA table_info(field_reservations)').fetchall()
    holder_pk = next((row['pk'] for row in columns if row['name'] == 'holder'), 0)
    if holder_pk == 0:
        db.execute('DROP INDEX IF EXISTS reservations_by_holder')
        db.execute('ALTER TABLE field_reservations RENAME TO field_reservations_legacy')
        db.execute('''CREATE TABLE field_reservations (
            volume_id TEXT NOT NULL REFERENCES originals(volume_id),
            row_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            holder TEXT NOT NULL REFERENCES correction_requests(request_id),
            PRIMARY KEY (volume_id, row_id, field_name, holder)
        )''')
        db.execute('''INSERT OR IGNORE INTO field_reservations
            (volume_id, row_id, field_name, holder)
            SELECT volume_id, row_id, field_name, holder FROM field_reservations_legacy''')
        db.execute('DROP TABLE field_reservations_legacy')
    db.execute('CREATE INDEX IF NOT EXISTS reservations_by_holder ON field_reservations(holder)')


def _existing_holders(db, item):
    return db.execute('''SELECT fr.holder AS holder, cr.mode AS mode, cf.target_value AS target_value
        FROM field_reservations fr
        JOIN correction_requests cr ON cr.request_id = fr.holder
        JOIN correction_fields cf
          ON cf.request_id = fr.holder AND cf.row_id = fr.row_id AND cf.field_name = fr.field_name
        WHERE fr.volume_id = ? AND fr.row_id = ? AND fr.field_name = ?
        ORDER BY fr.rowid''',
        (item.address.volume_id, item.address.row_id, item.field_name)).fetchall()


def reserve_fields(db, request_id, fields, *, shared=False):
    fields = tuple(fields)
    for item in fields:
        for holder_row in _existing_holders(db, item):
            same_value = holder_row['target_value'] == dump_reading(item.target)
            shared_with_peer = shared and holder_row['mode'] == 'shared' and same_value
            if not shared_with_peer:
                raise FieldsBusy(item.address, item.field_name, holder_row['holder'])
    db.executemany('INSERT INTO field_reservations VALUES (?, ?, ?, ?)',
                   ((item.address.volume_id, item.address.row_id, item.field_name, request_id) for item in fields))


def check_field_holder(db, address, field_name):
    """Return any one genuinely active holder, or None when the field is free."""
    entry = db.execute('''SELECT fr.holder AS holder FROM field_reservations fr
        JOIN correction_requests cr ON cr.request_id = fr.holder
        WHERE fr.volume_id = ? AND fr.row_id = ? AND fr.field_name = ?
          AND cr.state IN ('pending', 'cancelling')
        ORDER BY fr.rowid LIMIT 1''',
        (address.volume_id, address.row_id, field_name)).fetchone()
    return None if entry is None else entry['holder']


def field_held_by(db, address, field_name, request_id):
    entry = db.execute('''SELECT 1 FROM field_reservations
        WHERE volume_id = ? AND row_id = ? AND field_name = ? AND holder = ?''',
        (address.volume_id, address.row_id, field_name, request_id)).fetchone()
    return entry is not None


def active_confirmed_holders(db, address, field_name, *, exclude=None):
    """Active co-holders whose own prepare step durably confirms this field.

    A bare reservation (registered intent only) deliberately does not qualify:
    without a persisted word confirmation it cannot preserve the segment.
    """
    clause = '' if exclude is None else 'AND fr.holder <> ?'
    parameters = [address.volume_id, address.row_id, field_name]
    if exclude is not None:
        parameters.append(exclude)
    rows = db.execute(f'''SELECT fr.holder AS holder
        FROM field_reservations fr
        JOIN correction_requests cr ON cr.request_id = fr.holder
        WHERE fr.volume_id = ? AND fr.row_id = ? AND fr.field_name = ?
          AND cr.state IN ('pending', 'cancelling')
          {clause}
          AND EXISTS (
              SELECT 1 FROM correction_steps cs
              WHERE cs.request_id = fr.holder
                AND cs.row_id = fr.row_id
                AND cs.field_name = fr.field_name
                AND cs.phase = 'prepare')
        ORDER BY fr.rowid''', parameters).fetchall()
    return tuple(row['holder'] for row in rows)


def release_fields(db, request_id):
    db.execute('DELETE FROM field_reservations WHERE holder = ?', (request_id,))
