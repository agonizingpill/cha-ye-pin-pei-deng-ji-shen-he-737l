import pytest
from fastapi.testclient import TestClient

from src.db import Base, Session, engine
from src.main import app
from src.models import GradeReview, LotReservation, GradingOrder, BlendRecipe, TeaLot, User
from sqlalchemy import select, func


PASSWORD = 'Teacup1121!'


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    # 容器内 nginx 会把 /api 前缀剥离后转发，测试客户端直接打根路径。
    with TestClient(app, base_url='http://tearoom.test') as test_client:
        yield test_client


def token(client, username):
    response = client.post('/session', json={'username': username, 'password': PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()['token']


def auth(client, username):
    return {'Authorization': f'Bearer {token(client, username)}'}


def trial(client, username, grade, kg, note='定级订单试算备注'):
    return client.post('/orders/trial',
                       json={'target_grade': grade, 'kilograms': kg, 'note': note},
                       headers=auth(client, username))


def confirm(client, username, order_id):
    return client.post(f'/orders/{order_id}/confirm', headers=auth(client, username))


def reservation_count(db, state=None):
    query = select(func.count()).select_from(LotReservation)
    if state:
        query = query.where(LotReservation.state == state)
    return db.scalar(query)


def reservation_count_for_order(db, order_id, state=None):
    query = select(func.count()).select_from(LotReservation).where(LotReservation.order_id == order_id)
    if state:
        query = query.where(LotReservation.state == state)
    return db.scalar(query)


def lot_by_code(db, code):
    return db.scalar(select(TeaLot).where(TeaLot.code == code))


def held_for(db, lot_id):
    rows = db.scalars(select(LotReservation.kilograms).where(
        LotReservation.lot_id == lot_id, LotReservation.state == 'held')).all()
    from decimal import Decimal
    return sum(rows, Decimal('0'))


def run_worker():
    """同步消费所有排队中的审核，替代容器里的 Redis worker。"""
    from src.worker import consume
    while True:
        consume()
        with Session() as db:
            pending = db.scalar(
                select(func.count()).select_from(GradeReview).where(GradeReview.state == 'queued'))
        if not pending:
            break


def order_recipe_count(db):
    return db.scalar(select(func.count()).select_from(BlendRecipe).where(BlendRecipe.order_id.is_not(None)))
