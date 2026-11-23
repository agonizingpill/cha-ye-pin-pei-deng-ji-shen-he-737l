from datetime import date
from decimal import Decimal

from sqlalchemy import select

from src.auth import password_hash
from src.constants import ATTRIBUTES, Grade, Role
from src.db import Base, engine, transaction
from src.models import (
    BlendRecipe,
    BlendShare,
    GradeReview,
    GradingOrder,
    RetainedSample,
    SensoryPanel,
    TeaLot,
    ThresholdPolicy,
    User,
)
from src.workflow import confirm_order, freeze, trial_order, wake

ACCOUNTS = {'lin': ('林苇', Role.OPERATOR), 'he': ('何栀', Role.OPERATOR),
            'xu': ('许衡', Role.SUPERVISOR), 'view': ('周舟', Role.VIEWER)}
LOTS = [
    ('WUYI-1121-A', '肉桂', '马头岩', '中足火', 36.00, (94, 92, 90, 88, 1), '桂辛清晰，落水有厚度。'),
    ('WUYI-1121-B', '水仙', '慧苑坑', '中火', 48.50, (89, 94, 93, 91, 1), '木质香干净，回甘留得住。'),
    ('WUYI-1121-C', '奇兰', '莲花峰', '轻火', 25.00, (71, 70, 70, 71, 4), '花香明亮，尾段略收敛。'),
    ('WUYI-1121-D', '梅占', '青狮岩', '中火', 31.25, (65, 68, 67, 73, 4), '杯底蜜韵偏轻，汤感略薄。'),
    ('WUYI-1121-E', '铁罗汉', '内鬼洞', '足火', 18.50, (82, 79, 85, 80, 3), '焙火稳定，留样罐待上架。'),
]
RECIPES = [
    ('BL-1121-A', '岩骨桂香', [0, 1], [60, 40], '桂辛做前段，水仙保住中后段厚度。'),
    ('BL-1121-B', '兰韵试拼', [2, 3], [55, 45], '这组茶处于等级交界，复评时单独编号冲泡。'),
    ('BL-1121-C', '罗汉增厚试案', [1, 4], [55, 40], '等称重记录补齐后再封样送审。'),
]

# 定级订单演示：一张可确认的达线试算，一张无法达线、留下原因说明。
ORDER_TRIALS = [
    (Grade.SELECTION, Decimal('12.50'), '门店定级订单，要求精选级小批量拼配。'),
    (Grade.SECONDARY, Decimal('20.00'), '复配级补单，用来演示找不到达线配比时的原因留痕。'),
]


class _OrderTrial:
    def __init__(self, target_grade, kilograms, note):
        self.target_grade = target_grade
        self.kilograms = kilograms
        self.note = note


def seed():
    queued = []
    with transaction() as db:
        accounts = {}
        for username, (name, role) in ACCOUNTS.items():
            user = db.scalar(select(User).filter_by(username=username))
            if user is None:
                user = User(username=username, name=name, role=role, password_hash=password_hash('Teacup1121!'))
                db.add(user)
                db.flush()
            accounts[username] = user
        lin, he, xu = (accounts[key].id for key in ('lin', 'he', 'xu'))
        if db.get(ThresholdPolicy, 1) is None:
            db.add(ThresholdPolicy(id=1, floor=Decimal('.900'), reason='首班交界样保守复评线', author_id=xu))
        lot_ids = []
        for index, (code, cultivar, origin, roast, kg, scores, note) in enumerate(LOTS):
            lot = db.scalar(select(TeaLot).filter_by(code=code))
            if lot is None:
                lot = TeaLot(code=code, cultivar=cultivar, origin=origin, roast=roast,
                             kilograms=Decimal(str(kg)), received=date(2026, 11, 21), registered_by=lin)
                db.add(lot)
                db.flush()
                db.add(SensoryPanel(lot_id=lot.id, author_id=he, note=note, **dict(zip(ATTRIBUTES, scores))))
                if index < 4:
                    db.add(RetainedSample(seal=f'RL-1121-{index + 1}', lot_id=lot.id, grams=80,
                                          cabinet=f'A 柜 / {index + 1:02d} 格', sealed_by=he))
            lot_ids.append(lot.id)
        db.flush()
        for index, (code, name, lots, percents, note) in enumerate(RECIPES):
            if db.scalar(select(BlendRecipe.id).filter_by(code=code)) is not None:
                continue
            recipe = BlendRecipe(code=code, name=name, note=note, author_id=lin,
                                 shares=[BlendShare(lot_id=lot_ids[lot], percent=Decimal(percent))
                                         for lot, percent in zip(lots, percents)])
            db.add(recipe)
            db.flush()
            if index < 2:
                review = freeze(db, recipe)
                db.add(review)
                db.flush()
                queued.append(review.id)

        # 定级订单：第一张确认并留料生成草稿，第二张保留“找不到达线配比”的原因。
        if db.scalar(select(GradingOrder.id).limit(1)) is None:
            for index, (grade, kilograms, note) in enumerate(ORDER_TRIALS):
                order = trial_order(db, accounts['lin'], _OrderTrial(grade, kilograms, note))
                db.flush()
                if index == 0 and order.suggestion is not None:
                    confirm_order(db, accounts['lin'], order.id)
    for key in queued:
        wake(key)
    print(f'Demo seed complete. New grade reviews: {len(queued)}')


if __name__ == '__main__':
    Base.metadata.create_all(engine)
    seed()
