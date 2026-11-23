from enum import StrEnum


class Role(StrEnum):
    OPERATOR = 'operator'
    SUPERVISOR = 'supervisor'
    VIEWER = 'viewer'


class ReviewState(StrEnum):
    QUEUED = 'queued'
    FAILED = 'failed'
    REPANEL = 'repanel'
    VERIFY = 'verify'
    SIGNOFF = 'signoff'
    APPROVED = 'approved'
    SEALED = 'sealed'


class OrderState(StrEnum):
    TRIED = 'tried'          # 已试算，仅产生建议，未占料
    CONFIRMED = 'confirmed'  # 已留料并生成配方草稿
    CANCELLED = 'cancelled'  # 草稿撤销，留料退回


class ReserveState(StrEnum):
    HELD = 'held'            # 留料锁定中
    RELEASED = 'released'    # 草稿撤销，退回可用余量
    CONSUMED = 'consumed'    # 成品封存后正式核销


class Grade(StrEnum):
    SELECTION = 'selection'
    STANDARD = 'standard'
    SECONDARY = 'secondary'


ATTRIBUTES = ('aroma', 'body', 'sweetness', 'leaf', 'defect')
MODEL_SEED = 67
MODEL_VERSION = 'wuyi-panel-v1'

# 定级订单试算搜索参数
ORDER_MIN_LOTS = 2
ORDER_MAX_LOTS = 3
ORDER_STEP_BPS = 100          # 配比搜索步长 1.00%
ORDER_MIN_SHARE_BPS = 500     # 每批至少 5.00%
