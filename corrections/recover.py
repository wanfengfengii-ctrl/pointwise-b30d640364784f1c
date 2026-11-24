from dataclasses import dataclass

from archive_db import ConflictError, WordUnavailable, connection, require_name, transaction
from corrections.journal import (group_field_evidence, load_request, published_receipt, read_field_evidence,
                                 record_field_step, record_terminal, request_fields)
from corrections.pending import (active_confirmed_holders, check_field_holder, field_held_by, release_fields)
from corrections.publish import publish_correction
from intake.archive import get_original
from layout.codec import decode_field
from layout.registry import get_layout
from words.store import read_word
from words.write import apply_field_write


@dataclass(frozen=True)
class CancelledCorrection:
    request_id: str
    volume_id: str


def _load_work(db, request):
    original = get_original(db, request.volume_id)
    layout = get_layout(db, original.layout_id)
    return original, layout, request_fields(db, request.request_id)


def _field_maps(original, layout):
    rows = {row.row_id: row for row in original.rows}
    fields = {field.name: field for field in layout.fields}
    return rows, fields


def _formal_value(db, original, row_map, field_map, address, field_name):
    """Newest formally published target for the field, else its sealed original reading."""
    published = read_field_evidence(db, address, field_name, published_only=True)
    if published is not None:
        return published.target
    return decode_field(row_map[address.row_id].words[address.slot], field_map[field_name])


def _eligible_changes(db, request_id, address, fields, phase, original, row_map, field_map):
    expected_state = {'prepare': 'pending', 'cancel': 'cancelling', 'settle': 'published'}[phase]
    if load_request(db, request_id).state != expected_state:
        return {}
    changes = {}
    for item in fields:
        if phase == 'settle':
            # A field still reserved by any active ticket is that ticket's
            # responsibility; only replay published bits this receipt owns.
            if check_field_holder(db, address, item.field_name) is not None:
                continue
            published = read_field_evidence(db, address, item.field_name, published_only=True)
            if published is not None and published.request_id == request_id:
                changes[item.field_name] = item.target
            continue
        if not field_held_by(db, address, item.field_name, request_id):
            continue
        if phase == 'prepare':
            changes[item.field_name] = item.target
            continue
        # Cancellation: stand down on a segment another active ticket has
        # durably confirmed, so its bits stay intact. A bare same-value intent
        # has no prepare step and therefore offers no protection.
        protectors = active_confirmed_holders(db, address, item.field_name, exclude=request_id)
        if protectors:
            continue
        # Last confirmation leaving the field: restore the formal value visible
        # at this cleanup cross-section (latest publication, else original).
        changes[item.field_name] = _formal_value(
            db, original, row_map, field_map, address, item.field_name)
    return changes


def _drive_words(path, request, original, layout, facts, phase, hook):
    row_map, field_map = _field_maps(original, layout)
    for address, fields in group_field_evidence(facts):
        def qualify(db):
            return _eligible_changes(db, request.request_id, address, fields, phase,
                                     original, row_map, field_map)

        try:
            apply_field_write(path, layout, address, request.request_id, phase, qualify, record_field_step, hook=hook)
        except WordUnavailable:
            if phase != 'settle':
                raise
            # The signed receipt remains available while this address needs inspection.


def settle_published(path, request_id, *, hook=None):
    with connection(path) as db, transaction(db):
        request = load_request(db, request_id)
        receipt = published_receipt(request)
        original, layout, facts = _load_work(db, request)
    _drive_words(path, request, original, layout, facts, 'settle', hook)
    return receipt


def cancel_pending(path, request_id, *, hook=None):
    with connection(path) as db, transaction(db, write=True):
        request = load_request(db, request_id)
        result = CancelledCorrection(request_id, request.volume_id)
        if request.state == 'published':
            raise ConflictError(f'published request cannot be cancelled: {request_id}')
        if request.state == 'cancelled':
            return result
        if request.state == 'pending':
            record_terminal(db, request_id, 'cancelling')
        original, layout, facts = _load_work(db, request)
    _drive_words(path, request, original, layout, facts, 'cancel', hook)
    with connection(path) as db, transaction(db, write=True):
        if load_request(db, request_id).state == 'cancelled':
            return result
        original = get_original(db, request.volume_id)
        layout = get_layout(db, original.layout_id)
        row_map, field_map = _field_maps(original, layout)
        facts = request_fields(db, request_id)
        for address, fields in group_field_evidence(facts):
            for item in fields:
                if not field_held_by(db, address, item.field_name, request_id):
                    raise WordUnavailable(f'cleanup ownership not confirmed at {address}')
            word = read_word(db, address)
            for item in fields:
                formal = _formal_value(db, original, row_map, field_map, address, item.field_name)
                acceptable = {formal}
                # A surviving co-holder's confirmed same-value segment may
                # legitimately remain packed instead of the formal reading.
                if active_confirmed_holders(db, address, item.field_name, exclude=request_id):
                    acceptable.add(item.target)
                actual = decode_field(word, field_map[item.field_name])
                if actual not in acceptable:
                    raise WordUnavailable(f'cleanup readings differ at {address}')
        record_terminal(db, request_id, 'cancelled')
        release_fields(db, request_id)
    return result


def advance_correction(path, request_id, *, hook=None):
    with connection(path) as db, transaction(db):
        request = load_request(db, request_id)
        if request.state == 'pending':
            original, layout, facts = _load_work(db, request)
    if request.state == 'published':
        return settle_published(path, request_id, hook=hook)
    if request.state == 'cancelled':
        return CancelledCorrection(request_id, request.volume_id)
    if request.state == 'pending':
        _drive_words(path, request, original, layout, facts, 'prepare', hook)
        receipt = publish_correction(path, request_id, hook=hook)
        if receipt is not None:
            return receipt
    return cancel_pending(path, request_id, hook=hook)


def resume(path, request_id, *, action='continue', hook=None):
    require_name(request_id, 'request id')
    if action == 'continue':
        return advance_correction(path, request_id, hook=hook)
    if action == 'cancel':
        return cancel_pending(path, request_id, hook=hook)
    raise ValueError(f'unknown recovery action {action}')
