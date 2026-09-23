"""Run real Alembic DDL on the explicitly configured disposable MySQL database."""
import os
import pytest
from sqlalchemy import MetaData, inspect, text
from sqlalchemy.engine import make_url
from alembic.migration import MigrationContext
from alembic.autogenerate import compare_metadata
from flask_migrate import upgrade, downgrade
from app import create_app
from config import TestingConfig
from extensions import db

@pytest.mark.skipif(not os.environ.get('TEST_MYSQL_URL'), reason='requires disposable MySQL database')
def test_mysql_migrations_preserve_data_and_match_models():
    url = make_url(os.environ['TEST_MYSQL_URL'])
    assert url.database == 'shift_scheduler_test' and url.host in ('localhost','127.0.0.1')
    class MigrationTest(TestingConfig):
        SQLALCHEMY_DATABASE_URI = url
    app = create_app(MigrationTest)
    with app.app_context():
        metadata = MetaData()
        metadata.reflect(bind=db.engine)
        metadata.drop_all(bind=db.engine)
        try:
            upgrade(revision='9b5611f576b6')
            with db.engine.begin() as c:
                c.execute(text("INSERT INTO jobs (id,title,location_address,default_headcount,timezone) VALUES (1,'Migration audit','Test arena',2,'America/Denver')"))
                c.execute(text("INSERT INTO shifts (id,job_id,date,start_time,end_time,required_headcount,status,source) VALUES (1,1,'2026-09-20','09:00','17:00',2,'open','manual')"))
            upgrade()
            with db.engine.connect() as c:
                assert not compare_metadata(MigrationContext.configure(c,opts={'compare_type':True}),db.metadata)
                assert c.execute(text('SELECT end_time FROM shifts WHERE id=1')).scalar() is not None
            downgrade(revision='9b5611f576b6')
            upgrade()
            downgrade(revision='base')
            assert inspect(db.engine).get_table_names() == ['alembic_version']
        finally:
            db.session.remove()
            metadata = MetaData()
            metadata.reflect(bind=db.engine)
            metadata.drop_all(bind=db.engine)
            db.engine.dispose()
