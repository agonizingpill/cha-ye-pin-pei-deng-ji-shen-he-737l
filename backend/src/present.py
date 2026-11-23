from datetime import timezone

from sqlalchemy import select

from .constants import ATTRIBUTES, ReviewState
from .models import SensoryPanel, User
from .workflow import held_total, lot_state, sample_for


def iso(when):
    return when.replace(tzinfo=timezone.utc).isoformat(timespec='seconds') if when else None


def user_out(user):
    return {'id': user.id, 'username': user.username, 'name': user.name, 'role': user.role}


def panel_out(row, db):
    return {'id': row.id, 'author': db.get(User, row.author_id).name, 'grade': row.grade,
            'note': row.note, 'created_at': iso(row.created_at),
            'scores': {key: getattr(row, key) for key in ATTRIBUTES}}


def sample_out(row):
    return {'id': row.id, 'seal': row.seal, 'grams': row.grams, 'cabinet': row.cabinet,
            'lot_id': row.lot_id, 'review_id': row.review_id, 'sealed_at': iso(row.sealed_at)} if row else None


def lot_out(row, db):
    held = held_total(db, row.id)
    available = row.kilograms - held
    return {'id': row.id, 'code': row.code, 'cultivar': row.cultivar, 'origin': row.origin,
            'roast': row.roast, 'kilograms': str(row.kilograms), 'held_kg': str(held),
            'available_kg': str(available), 'received': row.received.isoformat(),
            'state': lot_state(db, row), 'panels': [panel_out(panel, db) for panel in reversed(row.panels)],
            'sample': sample_out(sample_for(db, lot_id=row.id))}


def review_out(row, db):
    panel = db.scalar(select(SensoryPanel).filter_by(review_id=row.id))
    result = row.result
    if row.state == ReviewState.REPANEL:
        result = {key: result[key] for key in ('confidence', 'effective_threshold', 'calibrated_floor',
                                                'model', 'seed', 'input_digest', 'validation')}
    return {'id': row.id, 'revision': row.revision, 'state': row.state, 'failure': row.failure,
            'snapshot': row.snapshot, 'digest': row.digest, 'result': result, 'attempts': row.attempts,
            'verified_by': db.get(User, row.verified_by).name if row.verified_by else None,
            'signed_by': db.get(User, row.signed_by).name if row.signed_by else None,
            'final_grade': row.final_grade if row.signed_by else None,
            'repanel': panel_out(panel, db) if panel else None,
            'sample': sample_out(sample_for(db, review_id=row.id))}


def reservation_out(row, db):
    return {'id': row.id, 'lot_id': row.lot_id, 'lot_code': row.lot.code,
            'kilograms': str(row.kilograms), 'state': row.state,
            'author': db.get(User, row.author_id).name,
            'created_at': iso(row.created_at), 'released_at': iso(row.released_at)}


def recipe_out(row, db):
    return {'id': row.id, 'code': row.code, 'name': row.name, 'revision': row.revision, 'note': row.note,
            'author_id': row.author_id, 'author': db.get(User, row.author_id).name,
            'order_id': row.order_id,
            'shares': [{'lot_id': share.lot_id, 'code': share.lot.code, 'cultivar': share.lot.cultivar,
                        'percent': str(share.percent), 'lot_state': lot_state(db, share.lot)} for share in row.shares],
            'reservations': [reservation_out(item, db) for item in row.reservations],
            'review': review_out(row.reviews[-1], db) if row.reviews else None}


def order_out(row, db):
    recipe = row.recipe
    return {'id': row.id, 'code': row.code, 'target_grade': row.target_grade,
            'kilograms': str(row.kilograms), 'note': row.note, 'state': row.state,
            'author': db.get(User, row.author_id).name,
            'suggestion': row.suggestion, 'failure': row.failure,
            'recipe_id': row.recipe_id,
            'recipe_code': recipe.code if recipe else None,
            'created_at': iso(row.created_at), 'confirmed_at': iso(row.confirmed_at)}


def policy_out(row, db):
    return {'id': row.id, 'floor': str(row.floor), 'reason': row.reason,
            'author': db.get(User, row.author_id).name, 'created_at': iso(row.created_at)}
