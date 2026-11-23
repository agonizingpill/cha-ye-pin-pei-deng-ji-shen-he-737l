import os

os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')

import sqlalchemy
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.pool import StaticPool


def _patched_create_engine(url, *args, **kwargs):
    if str(url).startswith('sqlite'):
        kwargs.pop('isolation_level', None)
        kwargs['connect_args'] = {'check_same_thread': False}
        kwargs['poolclass'] = StaticPool
    return sa_create_engine(url, *args, **kwargs)


sqlalchemy.create_engine = _patched_create_engine
