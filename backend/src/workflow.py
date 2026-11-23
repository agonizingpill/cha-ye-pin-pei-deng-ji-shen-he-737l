import logging
from decimal import Decimal

from fastapi import HTTPException
from redis import RedisError
from sqlalchemy import select

from . import order_solver
from .constants import ATTRIBUTES, OrderState, ReserveState, ReviewState as State
from .db import QUEUE, redis
from .grade_model import digest
from .models import (
    Audit,
    BlendRecipe,
    BlendShare,
    GradeReview,
    GradingOrder,
    LotReservation,
    RetainedSample,
    SensoryPanel,
    TeaLot,
    ThresholdPolicy,
    now,
)

logger = logging.getLogger(__name__)


def record(db, user, event, subject, explanation):
    db.add(Audit(actor=user.username if hasattr(user, 'username') else user, event=event,
                 subject=str(subject), explanation=explanation))


def require(db, model, key, lock=False):
    row = db.get(model, key, with_for_update=lock)
    if row is None:
        raise HTTPException(404, '记录不存在')
    return row


def sample_for(db, **reference):
    return db.scalar(select(RetainedSample).filter_by(**reference))


def lot_state(db, lot):
    if not lot.panels:
        return 'awaiting_panel'
    if not sample_for(db, lot_id=lot.id):
        return 'awaiting_sample'
    return 'ready'


def replace_shares(db, recipe, data):
    for share in data.shares:
        require(db, TeaLot, share.lot_id)
    incoming = {share.lot_id: share.percent for share in data.shares}
    for existing in list(recipe.shares):
        if existing.lot_id in incoming:
            existing.percent = incoming.pop(existing.lot_id)
        else:
            recipe.shares.remove(existing)
    recipe.shares.extend(BlendShare(lot_id=key, percent=percent) for key, percent in incoming.items())


def latest_policy(db):
    policy = db.scalar(select(ThresholdPolicy).order_by(ThresholdPolicy.id.desc()).limit(1))
    if policy is None:
        raise HTTPException(409, '请主管先登记模型阈值版本')
    return policy


def freeze(db, recipe):
    total = sum((share.percent for share in recipe.shares), Decimal('0'))
    if total != Decimal('100.00'):
        raise HTTPException(409, f'配比为 {total:.2f}%，必须闭合至 100.00%')
    ingredients = []
    for share in recipe.shares:
        lot = share.lot
        sample = sample_for(db, lot_id=lot.id)
        if sample is None:
            raise HTTPException(409, f'{lot.code} 缺失原料留样，封签登记后再送审')
        if not lot.panels:
            raise HTTPException(409, f'{lot.code} 缺失感官评审，请补录')
        panel = lot.panels[-1]
        ingredients.append({'lot': lot.code, 'percent': str(share.percent), 'seal': sample.seal,
                            'panel_id': panel.id, 'scores': {key: getattr(panel, key) for key in ATTRIBUTES}})
    policy = latest_policy(db)
    snapshot = {'code': recipe.code, 'revision': recipe.revision, 'ingredients': ingredients,
                'policy_version': policy.id, 'policy_floor': str(policy.floor)}
    return GradeReview(recipe_id=recipe.id, revision=recipe.revision, threshold_id=policy.id,
                       state=State.QUEUED, snapshot=snapshot, digest=digest(snapshot))


def wake(review_id):
    try:
        redis.rpush(QUEUE, review_id)
    except RedisError:
        logger.warning('Redis notification failed for review %s; database polling will recover', review_id)


def expect(review, *states):
    if review.state not in states:
        raise HTTPException(409, f'审核状态 {review.state} 不支持该操作')


def independent(db, review, user):
    recipe = require(db, BlendRecipe, review.recipe_id)
    if recipe.author_id == user.id:
        raise HTTPException(403, '配方作者不能复评或审核自己的配方')
    return recipe


def repanel(db, review, user, data):
    independent(db, review, user)
    expect(review, State.REPANEL)
    db.add(SensoryPanel(review_id=review.id, author_id=user.id, **data.model_dump()))
    review.state = State.VERIFY
    record(db, user, 'panel.recheck', review.id, data.note)


def verify(db, review, user, note):
    independent(db, review, user)
    expect(review, State.VERIFY)
    panel = db.scalar(select(SensoryPanel).filter_by(review_id=review.id))
    review.final_grade = panel.grade if panel else review.result['candidate']
    review.verified_by = user.id
    review.state = State.SIGNOFF
    record(db, user, 'grade.verify', review.id, note)


def approve(db, review, user, note):
    independent(db, review, user)
    expect(review, State.SIGNOFF)
    if review.verified_by == user.id:
        raise HTTPException(403, '签发和初审须由两人分别完成')
    review.signed_by = user.id
    review.state = State.APPROVED
    record(db, user, 'grade.signoff', review.id, note)


# ---------------------------------------------------------------------------
# 定级订单：试算不占料，确认才留料并生成配方草稿，撤销退回，封存核销
# ---------------------------------------------------------------------------

def held_total(db, lot_id):
    rows = db.scalars(
        select(LotReservation.kilograms).where(
            LotReservation.lot_id == lot_id, LotReservation.state == ReserveState.HELD
        )
    ).all()
    return sum(rows, Decimal('0'))


def _next_prefixed_code(db, model, prefix):
    """按编号前缀取最大流水号 + 1，避免重入 seed 或历史记录删改后撞号。"""
    codes = db.scalars(select(model.code).where(model.code.like(f'{prefix}-%'))).all()
    serial = 0
    for code in codes:
        tail = code.rpartition('-')[2]
        if tail.isdigit():
            serial = max(serial, int(tail))
    return f'{prefix}-{serial + 1:02d}'


def next_order_code(db):
    return _next_prefixed_code(db, GradingOrder, 'DO-1123')


def next_recipe_code(db):
    return _next_prefixed_code(db, BlendRecipe, 'BL-1123')


def trial_order(db, user, data):
    """试算：只读余量与判级模型，不产生任何留料。"""
    policy = latest_policy(db)
    kilograms = data.kilograms
    suggestion, failure = order_solver.search(db, data.target_grade, kilograms, policy.floor)
    order = GradingOrder(
        code=next_order_code(db), target_grade=data.target_grade, kilograms=kilograms,
        note=data.note, author_id=user.id, state=OrderState.TRIED,
        suggestion=suggestion, failure=failure,
    )
    db.add(order)
    db.flush()
    record(db, user, 'order.trial', order.code,
           f'目标 {data.target_grade} · {kilograms} kg · ' + ('已给出达线建议' if suggestion else failure['code']))
    return order


def confirm_order(db, user, order_id):
    """确认留料并生成配方草稿。整笔事务：余量不足则一份料也不占。"""
    order = require(db, GradingOrder, order_id, lock=True)

    # 幂等：重复确认直接返回既有草稿，不再占料、不再建草稿。
    if order.state == OrderState.CONFIRMED and order.recipe_id:
        record(db, user, 'order.confirm.idempotent', order.code, '重复确认已忽略，沿用既有留料草稿')
        return order.recipe
    if order.state != OrderState.TRIED or not order.suggestion:
        raise HTTPException(409, '该订单没有可确认的达线建议，请重新试算')

    suggestion = order.suggestion
    ingredients = suggestion['ingredients']
    amounts = suggestion['amounts']

    # 按批次主键顺序加行锁，避免并发事务交叉持锁造成死锁。
    lot_ids = sorted(row['lot_id'] for row in ingredients)
    lots = {lot.id: lot for lot in (require(db, TeaLot, lot_id, lock=True) for lot_id in lot_ids)}

    shortages = []
    for row, amount_text in zip(ingredients, amounts):
        amount = Decimal(amount_text)
        lot = lots[row['lot_id']]
        available = lot.kilograms - held_total(db, lot.id)
        if amount > available:
            shortages.append(f'{lot.code} 需 {amount} kg，可用仅 {available.quantize(Decimal("0.01"))} kg')
    if shortages:
        # 不写入任何留料或草稿，整笔回滚由调用方的事务边界保证。
        raise HTTPException(409, '余量不足，本单一份料也未占用：' + '；'.join(shortages) + '。请重新试算')

    recipe = BlendRecipe(
        code=next_recipe_code(db),
        name=f'{order.code} 定级订单草稿',
        note=order.note,
        author_id=user.id,
        order_id=order.id,
        shares=[
            BlendShare(lot_id=row['lot_id'], percent=Decimal(str(row['percent'])))
            for row in ingredients
        ],
    )
    db.add(recipe)
    db.flush()

    for row, amount_text in zip(ingredients, amounts):
        db.add(LotReservation(
            lot_id=row['lot_id'], recipe_id=recipe.id, order_id=order.id,
            kilograms=Decimal(amount_text), state=ReserveState.HELD, author_id=user.id,
        ))

    order.state = OrderState.CONFIRMED
    order.recipe_id = recipe.id
    order.confirmed_at = now()
    record(db, user, 'order.confirm', order.code,
           f'留料草稿 {recipe.code} · 锁定 {len(ingredients)} 批共 {order.kilograms} kg')
    return recipe


def cancel_recipe(db, user, recipe):
    """撤销留料草稿：所有 held 留料退回可用余量。已送审的草稿不可撤销。"""
    if recipe.reviews:
        raise HTTPException(409, '配方已送审冻结，不能撤销留料，请走审核流程')
    active = [row for row in recipe.reservations if row.state == ReserveState.HELD]
    if not active:
        raise HTTPException(409, '该配方没有锁定中的留料')
    for reservation in active:
        reservation.state = ReserveState.RELEASED
        reservation.released_at = now()
    if recipe.order_id:
        order = require(db, GradingOrder, recipe.order_id, lock=True)
        order.state = OrderState.CANCELLED
    record(db, user, 'recipe.cancel', recipe.code,
           f'撤销草稿，退回 {len(active)} 批留料共 '
           f'{sum((row.kilograms for row in active), Decimal("0"))} kg')


def consume_reservations(db, review, user):
    """成品封存后，把该配方锁定的留料正式核销。"""
    recipe = require(db, BlendRecipe, review.recipe_id)
    for reservation in recipe.reservations:
        if reservation.state == ReserveState.HELD:
            reservation.state = ReserveState.CONSUMED
    if recipe.order_id:
        record(db, user, 'reservation.consume', recipe.code, f'订单 {recipe.order_id} 留料随成品封存核销')
