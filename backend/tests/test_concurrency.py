"""并发守恒与幂等测试。

注意：SQLite 不支持 SELECT ... FOR UPDATE 行锁（with_for_update 被忽略），
真正的两人同时提交串行化由 MySQL/InnoDB 行锁保证；这里验证：
1. 连续确认多张订单时 held 总量永远不超过进货量，失败订单一份料不占；
2. 同一订单快速重试只产生一张草稿（应用层幂等分支）；
3. 数据库层 recipe_id 唯一约束兜底，禁止一单多草稿。
"""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.db import Session
from src.models import GradingOrder, TeaLot, User
from src.workflow import confirm_order, held_total

from helpers import client, reservation_count, reservation_count_for_order, trial  # noqa: F401
from src.constants import ReserveState


def _confirm(order_id, username='lin'):
    with Session.begin() as db:
        user = db.scalar(select(User).where(User.username == username))
        return confirm_order(db, user, order_id)


def test_confirm_chain_never_overdraw_and_failed_order_takes_nothing(client):
    # 连续试算确认同一等级订单，直到余量耗尽；验证守恒与“失败单一份料不占”的整笔语义。
    orders = [trial(client, 'lin', 'selection', '8.00').json()['id'] for _ in range(8)]
    failures = []

    def worker(order_id):
        try:
            _confirm(order_id)
        except Exception as exc:
            failures.append(str(exc))

    # SQLite 写库串行且无行锁，这里用顺序压测确定性验证守恒；
    # 真正的两人同时提交由 MySQL/InnoDB 行锁串行化（见 confirm_order 的 with_for_update）。
    for order_id in orders:
        worker(order_id)

    with Session() as db:
        for lot in db.scalars(select(TeaLot)):
            assert held_total(db, lot.id) <= lot.kilograms, f'{lot.code} 留料超过进货量'
        held = sum(reservation_count_for_order(db, oid, ReserveState.HELD.value) for oid in orders)
        expected = sum(
            len(db.get(GradingOrder, oid).suggestion['ingredients'])
            for oid in orders if db.get(GradingOrder, oid).state == 'confirmed'
        )
        assert held == expected
        states = [db.get(GradingOrder, oid).state for oid in orders]
        assert set(states) <= {'confirmed', 'tried'}
        for oid in orders:
            order = db.get(GradingOrder, oid)
            if order.state == 'tried':
                # 落后的失败单：一份料不占、没有草稿
                assert order.recipe_id is None
                assert reservation_count_for_order(db, oid) == 0
        assert failures, '连续占料后应当至少有一张订单因余量不足整笔失败'
        assert all('一份料也未占用' in message for message in failures)


def test_rapid_retry_returns_same_recipe(client):
    order_id = trial(client, 'lin', 'selection', '8.00').json()['id']
    first = _confirm(order_id)
    second = _confirm(order_id)  # 客户端超时后重试
    assert first.id == second.id
    with Session() as db:
        order = db.get(GradingOrder, order_id)
        assert order.recipe_id == first.id
        assert (reservation_count_for_order(db, order_id, ReserveState.HELD.value)
                == len(order.suggestion['ingredients']))


def test_database_unique_recipe_per_order(client):
    order_id = trial(client, 'lin', 'selection', '8.00').json()['id']
    _confirm(order_id)
    with Session.begin() as db:
        order = db.get(GradingOrder, order_id)
        order.recipe_id = order.recipe_id  # no-op 占位
        # 直接构造第二张草稿指向同一订单，必须被唯一约束拒绝
        from src.models import BlendRecipe
        rogue = BlendRecipe(code='BL-ROGUE-01', name='重复草稿', note='不应入库',
                            author_id=order.author_id, order_id=order_id)
        db.add(rogue)
        try:
            db.flush()
            raised = False
        except IntegrityError:
            raised = True
        assert raised, '一个订单只能关联一张配方草稿'
