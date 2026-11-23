from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, CheckConstraint, Date, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = 'tea_user'
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(60))
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(180))


class TeaLot(Base):
    __tablename__ = 'tea_lot'
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    cultivar: Mapped[str] = mapped_column(String(60))
    origin: Mapped[str] = mapped_column(String(100))
    received: Mapped[date] = mapped_column(Date)
    kilograms: Mapped[Decimal] = mapped_column(Numeric(9, 2))
    roast: Mapped[str] = mapped_column(String(40))
    registered_by: Mapped[int] = mapped_column(ForeignKey('tea_user.id'))
    panels: Mapped[list['SensoryPanel']] = relationship(order_by='SensoryPanel.id')


class BlendRecipe(Base):
    __tablename__ = 'blend_recipe'
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    author_id: Mapped[int] = mapped_column(ForeignKey('tea_user.id'))
    note: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(default=1)
    order_id: Mapped[int | None] = mapped_column(ForeignKey('grading_order.id'), unique=True)
    shares: Mapped[list['BlendShare']] = relationship(cascade='all, delete-orphan', order_by='BlendShare.lot_id')
    reviews: Mapped[list['GradeReview']] = relationship(order_by='GradeReview.id')
    reservations: Mapped[list['LotReservation']] = relationship(back_populates='recipe')


class BlendShare(Base):
    __tablename__ = 'blend_share'
    recipe_id: Mapped[int] = mapped_column(ForeignKey('blend_recipe.id'), primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey('tea_lot.id'), primary_key=True)
    percent: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    lot: Mapped[TeaLot] = relationship()
    __table_args__ = (CheckConstraint('percent > 0 AND percent <= 100'),)


class GradingOrder(Base):
    """定级订单：目标等级 + 公斤数，试算只给建议，确认后才留料并生成配方草稿。"""
    __tablename__ = 'grading_order'
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    target_grade: Mapped[str] = mapped_column(String(20))
    kilograms: Mapped[Decimal] = mapped_column(Numeric(9, 2))
    note: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int] = mapped_column(ForeignKey('tea_user.id'))
    state: Mapped[str] = mapped_column(String(20), index=True, default='tried')
    suggestion: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    failure: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    recipe_id: Mapped[int | None] = mapped_column(
        ForeignKey('blend_recipe.id', use_alter=True, name='fk_order_recipe'), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    recipe: Mapped['BlendRecipe | None'] = relationship(foreign_keys=[recipe_id], post_update=True)


class LotReservation(Base):
    """留料台账：held 锁定中、released 撤销退回、consumed 成品封存核销。"""
    __tablename__ = 'lot_reservation'
    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey('tea_lot.id'))
    recipe_id: Mapped[int] = mapped_column(ForeignKey('blend_recipe.id'))
    order_id: Mapped[int] = mapped_column(ForeignKey('grading_order.id'))
    kilograms: Mapped[Decimal] = mapped_column(Numeric(9, 2))
    state: Mapped[str] = mapped_column(String(20), index=True, default='held')
    author_id: Mapped[int] = mapped_column(ForeignKey('tea_user.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    released_at: Mapped[datetime | None] = mapped_column(DateTime)
    lot: Mapped[TeaLot] = relationship()
    recipe: Mapped[BlendRecipe] = relationship(back_populates='reservations')
    __table_args__ = (
        UniqueConstraint('recipe_id', 'lot_id', name='uq_reservation_recipe_lot'),
        CheckConstraint('kilograms > 0'),
    )


class ThresholdPolicy(Base):
    __tablename__ = 'grade_threshold'
    id: Mapped[int] = mapped_column(primary_key=True)
    floor: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    reason: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int] = mapped_column(ForeignKey('tea_user.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class GradeReview(Base):
    __tablename__ = 'grade_review'
    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey('blend_recipe.id'))
    revision: Mapped[int]
    threshold_id: Mapped[int] = mapped_column(ForeignKey('grade_threshold.id'))
    state: Mapped[str] = mapped_column(String(20), index=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    verified_by: Mapped[int | None] = mapped_column(ForeignKey('tea_user.id'))
    signed_by: Mapped[int | None] = mapped_column(ForeignKey('tea_user.id'))
    final_grade: Mapped[str | None] = mapped_column(String(20))
    failure: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint('recipe_id', 'revision'),)


class SensoryPanel(Base):
    __tablename__ = 'sensory_panel'
    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey('tea_lot.id'))
    review_id: Mapped[int | None] = mapped_column(ForeignKey('grade_review.id'), unique=True)
    author_id: Mapped[int] = mapped_column(ForeignKey('tea_user.id'))
    aroma: Mapped[int]
    body: Mapped[int]
    sweetness: Mapped[int]
    leaf: Mapped[int]
    defect: Mapped[int]
    note: Mapped[str] = mapped_column(Text)
    grade: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (CheckConstraint('(lot_id IS NULL) <> (review_id IS NULL)'),)


class RetainedSample(Base):
    __tablename__ = 'retained_sample'
    id: Mapped[int] = mapped_column(primary_key=True)
    seal: Mapped[str] = mapped_column(String(40), unique=True)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey('tea_lot.id'), unique=True)
    review_id: Mapped[int | None] = mapped_column(ForeignKey('grade_review.id'), unique=True)
    grams: Mapped[int]
    cabinet: Mapped[str] = mapped_column(String(60))
    sealed_by: Mapped[int] = mapped_column(ForeignKey('tea_user.id'))
    sealed_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (CheckConstraint('(lot_id IS NULL) <> (review_id IS NULL)'),)


class Audit(Base):
    __tablename__ = 'tea_audit'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(40))
    event: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(190))
    explanation: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
