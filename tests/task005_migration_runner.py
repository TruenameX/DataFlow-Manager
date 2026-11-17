"""Explicitly gated online migrations; import performs no connection or SQL."""

from dataclasses import replace
import importlib.util
from pathlib import Path


def migrate(environment, preparation_password, *, factory=None, upgrade=None):
    """Run approved upgrade on the approved test DB as its preparation role."""
    spec = importlib.util.spec_from_file_location("_task005_migration_gate", Path(__file__).parent / "integration" / "conftest.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    if (environment.get("DFM_TEST_MIGRATIONS_APPROVED") != "YES"
            or environment.get("DFM_TEST_DROP_CONSTRAINT_APPROVED") != "YES"
            or type(preparation_password) is not str or not preparation_password
            or "\x00" in preparation_password):
        raise RuntimeError("TASK005_MIGRATION_NOT_AUTHORIZED")
    config, _ = gate.prepare_resources(environment, lambda config: None)
    prep_config = replace(config, user=environment["DFM_TEST_PREPARATION_ROLE"],
                          password=preparation_password)
    if factory is None:
        from dataflow_manager.db.session import _create_intermediate_resources
        factory = _create_intermediate_resources
    if upgrade is None:
        from alembic.command import upgrade
    engine = None
    failed = False
    try:
        engine, _ = factory(prep_config)
        gate.protect_driver_logging(engine)
        gate.verify_identity(engine, prep_config, environment)
        from alembic.config import Config
        with engine.begin() as connection:
            alembic = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            alembic.attributes["connection"] = connection
            upgrade(alembic, "0002_host_policy")
    except Exception:
        failed = True
    finally:
        if engine is not None:
            try:
                gate.dispose_safely(engine)
            except Exception:
                failed = True
    if failed:
        raise RuntimeError("TASK005_MIGRATION_FAILED")
