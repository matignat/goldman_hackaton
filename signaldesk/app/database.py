import os
from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:////tmp/signaldesk.sqlite')
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True,
                       connect_args={'check_same_thread': False} if DATABASE_URL.startswith('sqlite') else {},
                       **({'pool_size': 10, 'max_overflow': 20} if not DATABASE_URL.startswith('sqlite') else {}))
if DATABASE_URL.startswith('sqlite'):
    @event.listens_for(engine, 'connect')
    def sqlite_pragmas(connection, record):
        cursor = connection.cursor()
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.execute('PRAGMA busy_timeout=5000')
        cursor.execute('PRAGMA foreign_keys=ON')
        cursor.close()

Session = sessionmaker(engine, expire_on_commit=False)
class Base(DeclarativeBase): pass

@contextmanager
def session_scope():
    db = Session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def init_db():
    from . import models
    Base.metadata.create_all(engine)
