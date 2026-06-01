from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def temp_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point the app at a throwaway sqlite file and init the schema."""
    from plex_get import db as dbmod
    from plex_get import manager as mgrmod
    db_file = tmp_path / 'plex-get.db'
    monkeypatch.setenv('DATABASE_PATH', str(db_file))
    dbmod.get_settings.cache_clear()
    new_engine = dbmod.create_engine(f'sqlite:///{db_file}', connect_args={'check_same_thread': False}, future=True)
    new_session = dbmod.sessionmaker(bind=new_engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(dbmod, 'engine', new_engine)
    monkeypatch.setattr(dbmod, 'SessionLocal', new_session)
    monkeypatch.setattr(mgrmod, 'SessionLocal', new_session)
    from plex_get.db import init_db
    init_db()
    yield dbmod
