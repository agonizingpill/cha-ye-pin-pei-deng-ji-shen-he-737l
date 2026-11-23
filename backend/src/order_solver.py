"""定级订单试算：在已品评、已封签的余料中搜索达到目标等级的配比。

试算只读余量，不做任何留料；确认动作在 workflow 中另起事务并加行锁。
搜索过程确定、可复现：先按原料编号排序组合，再按置信度与用料公斤数择优。
"""
from decimal import Decimal
from itertools import combinations

import numpy as np
from sqlalchemy import select

from . import grade_model
from .constants import (
    ATTRIBUTES,
    ORDER_MAX_LOTS,
    ORDER_MIN_LOTS,
    ORDER_MIN_SHARE_BPS,
    ORDER_STEP_BPS,
    ReserveState,
)
from .models import LotReservation, RetainedSample, SensoryPanel, TeaLot

TOTAL_BPS = 10_000


def held_kilograms(db, lot_id):
    """该批次当前仍锁定（held）的留料合计。"""
    rows = db.scalars(
        select(LotReservation.kilograms).where(
            LotReservation.lot_id == lot_id, LotReservation.state == ReserveState.HELD
        )
    ).all()
    return sum(rows, Decimal('0'))


def available_lots(db):
    """仅纳入已品评且已封签的原料批，并附带当前可用余量。"""
    lots = []
    for lot in db.scalars(select(TeaLot).order_by(TeaLot.id)):
        panel = db.scalar(select(SensoryPanel).filter_by(lot_id=lot.id).order_by(SensoryPanel.id.desc()))
        sample = db.scalar(select(RetainedSample).filter_by(lot_id=lot.id))
        if panel is None or sample is None:
            continue
        held = held_kilograms(db, lot.id)
        remaining = lot.kilograms - held
        if remaining <= 0:
            continue
        lots.append({
            'lot': lot, 'panel': panel, 'sample': sample,
            'remaining': remaining,
            'scores': {key: getattr(panel, key) for key in ATTRIBUTES},
        })
    return lots


def split_plans(size):
    """生成 size 个原料的整数基点配比，闭合 10000 基点，步长 100（1%），单项不少于 5%。"""
    minimum = ORDER_MIN_SHARE_BPS
    step = ORDER_STEP_BPS
    if size == 2:
        for first in range(minimum, TOTAL_BPS - minimum + 1, step):
            yield (first, TOTAL_BPS - first)
        return
    if size == 3:
        for first in range(minimum, TOTAL_BPS - 2 * minimum + 1, step):
            for second in range(minimum, TOTAL_BPS - first - minimum + 1, step):
                third = TOTAL_BPS - first - second
                if third >= minimum:
                    yield (first, second, third)
        return
    raise ValueError(f'暂不支持 {size} 批原料的搜索')


def build_ingredients(picked, bps_plan):
    ingredients = []
    for item, bps in zip(picked, bps_plan):
        ingredients.append({
            'lot_id': item['lot'].id, 'lot': item['lot'].code,
            'percent': bps / 100, 'scores': item['scores'],
        })
    return ingredients


def needs_for(bps_plan, kilograms):
    """每个原料按配比所需公斤数（Decimal，两位小数，差额并入最大份额以闭合）。"""
    from decimal import ROUND_HALF_UP
    raw = [kilograms * Decimal(bps) / Decimal(TOTAL_BPS) for bps in bps_plan]
    amounts = [value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) for value in raw]
    drift = (kilograms - sum(amounts, Decimal('0'))).quantize(Decimal('0.01'))
    if drift:
        leader = max(range(len(amounts)), key=lambda index: bps_plan[index])
        amounts[leader] += drift
    return amounts


def search(db, target_grade, kilograms, policy_floor):
    """返回 (suggestion, failure)。找到达线配比时 failure 为 None。"""
    eligible = available_lots(db)
    if not eligible:
        return None, {
            'code': 'no_eligible_lot',
            'message': '没有同时完成感官品评与封签的原料余料，先补录品评或封签。',
            'short_lots': [],
        }

    model, calibrated_floor, _ = grade_model.calibrated_model()
    effective_threshold = max(calibrated_floor, float(policy_floor))
    class_names = list(model.classes_)

    # 先收集通过余量约束的可行方案，再批量送模型评估，避免上万次单条预测。
    feasible = []
    for size in range(ORDER_MIN_LOTS, ORDER_MAX_LOTS + 1):
        for picked in combinations(eligible, size):
            score_matrix = np.asarray(
                [[item['scores'][field] for field in ATTRIBUTES] for item in picked], dtype=float
            )
            for bps_plan in split_plans(size):
                amounts = needs_for(bps_plan, kilograms)
                if any(amount > item['remaining'] for item, amount in zip(picked, amounts)):
                    continue
                weights = np.asarray(bps_plan, dtype=float) / TOTAL_BPS
                combined = weights @ score_matrix
                aroma_spread = (weights @ ((score_matrix[:, 0] - combined[0]) ** 2)) ** .5
                feasible.append({
                    'picked': picked, 'bps_plan': bps_plan, 'amounts': amounts,
                    'vector': np.r_[combined, aroma_spread],
                })

    if not feasible:
        return None, {
            'code': 'insufficient_remaining',
            'message': '余料公斤数不足以支撑该订单，任何闭合配比都超出可用余量，请补料后再试算。',
            'short_lots': [
                {'lot_id': item['lot'].id, 'lot': item['lot'].code, 'remaining': str(item['remaining'])}
                for item in eligible
            ],
        }

    matrix = np.vstack([item['vector'] for item in feasible])
    probabilities = model.predict_proba(matrix)
    indices = probabilities.argmax(axis=1)
    confidence = probabilities.max(axis=1)

    candidates = []
    for item, probs, index, conf in zip(feasible, probabilities, indices, confidence):
        candidate_grade = str(class_names[index])
        if candidate_grade != target_grade or float(conf) < effective_threshold:
            continue
        picked, bps_plan, amounts = item['picked'], item['bps_plan'], item['amounts']
        ingredients = build_ingredients(picked, bps_plan)
        candidates.append({
            'ingredients': ingredients,
            'amounts': [str(amount) for amount in amounts],
            'bps_plan': bps_plan,
            'confidence': float(conf),
            'probabilities': {grade: round(float(p), 6) for grade, p in zip(class_names, probs)},
            'effective_threshold': effective_threshold,
            'features': dict(zip((*ATTRIBUTES, 'aroma_spread'), item['vector'].round(4).tolist())),
            'lot_count': len(picked),
            'total_kg': str(kilograms),
        })

    if not candidates:
        return None, {
            'code': 'no_qualifying_mix',
            'message': '现有已品评、已封签余料按可用余量拼配后，模型候选等级或置信度未达到目标等级线。',
            'short_lots': [
                {'lot_id': item['lot'].id, 'lot': item['lot'].code, 'remaining': str(item['remaining'])}
                for item in eligible
            ],
        }

    # 置信度优先，其次用料更省、批次数更少，最后按原料编号与配比确定次序，保证结果可复现。
    def ranking(candidate):
        signature = tuple(
            (row['lot'], bps) for row, bps in zip(candidate['ingredients'], candidate['bps_plan'])
        )
        return (
            round(candidate['confidence'], 6) * -1,
            sum(Decimal(value) for value in candidate['amounts']),
            candidate['lot_count'],
            signature,
        )

    best = sorted(candidates, key=ranking)[0]
    best.pop('bps_plan', None)
    best['target_grade'] = target_grade
    best['kilograms'] = str(kilograms)
    return best, None
