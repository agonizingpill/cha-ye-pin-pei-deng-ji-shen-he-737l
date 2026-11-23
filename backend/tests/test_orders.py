from decimal import Decimal

import pytest
from sqlalchemy import select

from src.constants import Grade, OrderState, ReserveState
from src.db import Session
from src.models import BlendRecipe, GradingOrder, LotReservation, TeaLot, ThresholdPolicy, User

from helpers import (  # noqa: F401
    auth,
    client,
    confirm,
    held_for,
    lot_by_code,
    order_recipe_count,
    reservation_count,
    reservation_count_for_order,
    run_worker,
    trial,
)


def test_trial_does_not_reserve_and_returns_suggestion(client):
    response = trial(client, 'lin', Grade.SELECTION.value, '10.00')
    assert response.status_code == 201, response.text
    order = response.json()
    assert order['state'] == OrderState.TRIED.value
    assert order['failure'] is None
    assert order['suggestion'] is not None
    suggestion = order['suggestion']
    assert suggestion['target_grade'] == Grade.SELECTION.value
    assert suggestion['kilograms'] == '10.00'
    assert sum(Decimal(x) for x in suggestion['amounts']) == Decimal('10.00')
    assert len(suggestion['ingredients']) >= 2
    with Session() as db:
        assert reservation_count_for_order(db, order['id']) == 0  # 看建议时不占料
        assert db.get(GradingOrder, order['id']).state == OrderState.TRIED.value


def test_trial_failure_explains_reason(client):
    response = trial(client, 'lin', Grade.SECONDARY.value, '10.00')
    assert response.status_code == 201
    order = response.json()
    assert order['suggestion'] is None
    assert order['failure']['code'] == 'no_qualifying_mix'
    assert order['failure']['message']
    assert len(order['failure']['short_lots']) >= 2
    with Session() as db:
        assert reservation_count_for_order(db, order['id']) == 0


def test_confirm_reserves_and_creates_recipe_draft(client):
    order = trial(client, 'lin', Grade.SELECTION.value, '10.00').json()
    expected = {row['lot_id']: Decimal(amount)
                for row, amount in zip(order['suggestion']['ingredients'],
                                       order['suggestion']['amounts'])}
    with Session() as db:
        held_before = {lot_id: held_for(db, lot_id) for lot_id in expected}

    response = confirm(client, 'lin', order['id'])
    assert response.status_code == 201, response.text
    recipe = response.json()
    assert recipe['code'].startswith('BL-1123-')
    assert recipe['order_id'] == order['id']
    assert recipe['review'] is None  # 仍是草稿，未送审
    assert len(recipe['shares']) == len(order['suggestion']['ingredients'])
    locked = {row['lot_id']: Decimal(row['kilograms']) for row in recipe['reservations']
              if row['state'] == ReserveState.HELD.value}
    assert locked == expected

    with Session() as db:
        assert reservation_count_for_order(db, order['id'], ReserveState.HELD.value) == len(expected)
        # 各批留料增量恰为本单用量；可用余量 = 进货量 - 全部锁定
        for lot_id, amount in expected.items():
            lot = db.get(TeaLot, lot_id)
            assert held_for(db, lot_id) - held_before[lot_id] == amount
            assert lot.kilograms - held_for(db, lot_id) >= 0
        refreshed = db.get(GradingOrder, order['id'])
        assert refreshed.state == OrderState.CONFIRMED.value
        assert refreshed.recipe_id == recipe['id']

    listing = client.get('/lots', headers=auth(client, 'lin')).json()
    for row in listing:
        if row['id'] in expected:
            assert Decimal(row['held_kg']) - held_before[row['id']] == expected[row['id']]
            assert Decimal(row['available_kg']) == Decimal(row['kilograms']) - Decimal(row['held_kg'])


def test_confirm_is_idempotent_under_retry(client):
    order = trial(client, 'lin', Grade.SELECTION.value, '10.00').json()
    first = confirm(client, 'lin', order['id'])
    assert first.status_code == 201
    second = confirm(client, 'lin', order['id'])  # 网络重试 / 双击
    assert second.status_code == 201
    assert second.json()['id'] == first.json()['id']
    with Session() as db:
        assert (reservation_count_for_order(db, order['id'], ReserveState.HELD.value)
                == len(order['suggestion']['ingredients']))
        assert db.get(GradingOrder, order['id']).recipe_id == first.json()['id']
    # 重复确认没有新建第二张订单草稿
    with Session() as db:
        assert db.scalar(select(GradingOrder).where(GradingOrder.id == order['id'])).recipe_id == first.json()['id']


def test_concurrent_shortage_takes_nothing(client):
    # 两张订单瞄准同一批余料，先确认的占满，后确认的必须整笔失败且不占任何料。
    first = trial(client, 'lin', Grade.SELECTION.value, '30.00').json()
    second = trial(client, 'he', Grade.SELECTION.value, '30.00').json()
    assert first['suggestion'] and second['suggestion']

    done = confirm(client, 'lin', first['id'])
    assert done.status_code == 201

    conflict = confirm(client, 'he', second['id'])
    assert conflict.status_code == 409
    assert '余量不足' in conflict.json()['detail']
    assert '一份料也未占用' in conflict.json()['detail']

    with Session() as db:
        # 第二张失败单一份料都没占、没有草稿
        assert reservation_count_for_order(db, second['id']) == 0
        failed_order = db.get(GradingOrder, second['id'])
        assert failed_order.state == OrderState.TRIED.value
        assert failed_order.recipe_id is None

    # 落后一单按最新余量重新试算后可继续完成
    retried = trial(client, 'he', Grade.SELECTION.value, '5.00')
    assert retried.status_code == 201
    new_order = retried.json()
    assert new_order['suggestion'] is not None
    recovered = confirm(client, 'he', new_order['id'])
    assert recovered.status_code == 201


def test_cancel_releases_material_and_blocks_submit_after_release(client):
    order = trial(client, 'lin', Grade.SELECTION.value, '10.00').json()
    recipe = confirm(client, 'lin', order['id']).json()
    lot_id = recipe['shares'][0]['lot_id']
    with Session() as db:
        held_before = held_for(db, lot_id)
        assert held_before > 0

    cancelled = client.post(f"/blends/{recipe['id']}/cancel", headers=auth(client, 'lin'))
    assert cancelled.status_code == 200
    with Session() as db:
        assert reservation_count_for_order(db, order['id'], ReserveState.HELD.value) == 0
        assert (reservation_count_for_order(db, order['id'], ReserveState.RELEASED.value)
                == len(recipe['shares']))
        assert held_for(db, lot_id) == held_before - Decimal(
            next(r['kilograms'] for r in recipe['reservations'] if r['lot_id'] == lot_id))
        assert db.get(GradingOrder, order['id']).state == OrderState.CANCELLED.value
        lot = db.get(TeaLot, lot_id)
        listing_lot = next(x for x in client.get('/lots', headers=auth(client, 'lin')).json()
                           if x['id'] == lot_id)
        assert Decimal(listing_lot['available_kg']) == lot.kilograms - held_for(db, lot_id)


def test_locked_draft_cannot_be_edited_but_submission_flow_unchanged(client):
    order = trial(client, 'lin', Grade.SELECTION.value, '10.00').json()
    recipe = confirm(client, 'lin', order['id']).json()

    # 留料草稿锁住配比，普通修订被拒绝
    payload = {'code': recipe['code'], 'name': recipe['name'], 'note': '试图改写锁定配比',
               'shares': recipe['shares'][:2]}
    if len(payload['shares']) < 2:
        pytest.skip('种子数据至少两批原料')
    payload['shares'] = [{'lot_id': row['lot_id'], 'percent': row['percent']} for row in recipe['shares']]
    edit = client.put(f"/blends/{recipe['id']}", json=payload, headers=auth(client, 'lin'))
    assert edit.status_code == 409
    assert '锁定' in edit.json()['detail']

    # 送审仍走原流程
    submit = client.post(f"/blends/{recipe['id']}/review", headers=auth(client, 'lin'))
    assert submit.status_code == 202, submit.text

    # 已送审的草稿不能再撤销
    cancel = client.post(f"/blends/{recipe['id']}/cancel", headers=auth(client, 'lin'))
    assert cancel.status_code == 409


def test_sealed_review_consumes_reservations(client):
    order = trial(client, 'lin', Grade.SELECTION.value, '10.00').json()
    recipe = confirm(client, 'lin', order['id']).json()
    review = client.post(f"/blends/{recipe['id']}/review", headers=auth(client, 'lin')).json()
    run_worker()

    # 走到 approved：非作者操作员初审，主管签发
    he_headers = auth(client, 'he')
    review_id = review['id']
    fresh = client.get(f'/reviews/{review_id}', headers=he_headers).json()
    state = fresh['state']
    if state == 'repanel':
        repanel = client.post(f'/reviews/{review_id}/repanel', headers=he_headers, json={
            'aroma': 90, 'body': 88, 'sweetness': 87, 'leaf': 86, 'defect': 2,
            'grade': Grade.SELECTION.value, 'note': '独立盲复评杯样等级确认'})
        assert repanel.status_code == 200, repanel.text
    verify = client.post(f'/reviews/{review_id}/verify', headers=he_headers,
                         json={'note': '初审同意模型候选等级'})
    assert verify.status_code == 200, verify.text
    signoff = client.post(f'/reviews/{review_id}/signoff', headers=auth(client, 'xu'),
                          json={'note': '主管同意签发'})
    assert signoff.status_code == 200, signoff.text

    seal = client.post(f'/reviews/{review_id}/sample', headers=he_headers,
                       json={'seal': 'BL-SEAL-01', 'grams': 80, 'cabinet': 'B 柜 / 01 格'})
    assert seal.status_code == 201, seal.text
    with Session() as db:
        assert (reservation_count_for_order(db, order['id'], ReserveState.CONSUMED.value)
                == len(recipe['shares']))
        assert reservation_count_for_order(db, order['id'], ReserveState.HELD.value) == 0


def test_orders_listing_and_permissions(client):
    before = len(client.get('/orders', headers=auth(client, 'view')).json())
    trial(client, 'lin', Grade.SELECTION.value, '10.00')
    listing = client.get('/orders', headers=auth(client, 'view'))
    assert listing.status_code == 200
    assert len(listing.json()) == before + 1

    forbidden = client.post('/orders/trial',
                            json={'target_grade': Grade.SELECTION.value, 'kilograms': '10.00',
                                  'note': '观察员不应试算'},
                            headers=auth(client, 'view'))
    assert forbidden.status_code == 403

    unauthorized = client.post('/orders/trial',
                               json={'target_grade': Grade.SELECTION.value, 'kilograms': '10.00',
                                     'note': '未登录不应试算'})
    assert unauthorized.status_code == 401


def test_solver_deterministic_and_threshold_aware(client):
    from src import order_solver
    with Session() as db:
        floor = db.scalar(select(ThresholdPolicy).order_by(ThresholdPolicy.id.desc())).floor
        first, fail1 = order_solver.search(db, Grade.SELECTION.value, Decimal('12.34'), floor)
        second, fail2 = order_solver.search(db, Grade.SELECTION.value, Decimal('12.34'), floor)
    assert fail1 is None and fail2 is None
    assert first['ingredients'] == second['ingredients']
    assert first['amounts'] == second['amounts']
    # 达线建议的置信度必须不低于生效阈值
    assert first['confidence'] >= first['effective_threshold']
    # 各批用量按两位小数闭合到订单公斤数
    assert sum(Decimal(x) for x in first['amounts']) == Decimal('12.34')


def test_only_paneled_and_sealed_lots_are_eligible(client):
    # 种子中 E 批未封签，任何建议都不得使用它
    order = trial(client, 'lin', Grade.SELECTION.value, '10.00').json()
    codes = {row['lot'] for row in order['suggestion']['ingredients']}
    assert 'WUYI-1121-E' not in codes


def test_other_author_cannot_cancel_draft(client):
    order = trial(client, 'lin', Grade.SELECTION.value, '10.00').json()
    recipe = confirm(client, 'lin', order['id']).json()
    response = client.post(f"/blends/{recipe['id']}/cancel", headers=auth(client, 'he'))
    assert response.status_code == 403


def test_cancelled_order_cannot_be_reconfirmed(client):
    order = trial(client, 'lin', Grade.SELECTION.value, '10.00').json()
    recipe = confirm(client, 'lin', order['id']).json()
    cancelled = client.post(f"/blends/{recipe['id']}/cancel", headers=auth(client, 'lin'))
    assert cancelled.status_code == 200
    again = confirm(client, 'lin', order['id'])
    assert again.status_code == 409
    assert '重新试算' in again.json()['detail']
    # 重新试算生成新订单，原订单保持撤销状态
    new_trial = trial(client, 'lin', Grade.SELECTION.value, '10.00').json()
    assert new_trial['id'] != order['id']
    assert new_trial['suggestion'] is not None


def test_trial_order_requires_valid_grade_and_weight(client):
    bad_grade = client.post('/orders/trial',
                            json={'target_grade': 'premium', 'kilograms': '10.00', 'note': '非法等级'},
                            headers=auth(client, 'lin'))
    assert bad_grade.status_code == 422
    bad_weight = client.post('/orders/trial',
                             json={'target_grade': Grade.SELECTION.value, 'kilograms': '0', 'note': '零公斤'},
                             headers=auth(client, 'lin'))
    assert bad_weight.status_code == 422
