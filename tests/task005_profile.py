"""Offline Test Profile v1.0 preflight; does not authorize or open connections.

Caller-supplied identifiers and approval are assertions, not evidence that a
server, database, role or privilege set is isolated or owner-approved.
"""

import re

from dataflow_manager.db.config import ConfigError, load_database_config


def validate_profile(environment, *, approved, approved_database, approved_test_role, preparation_role,
                     excluded_resources):
    """Validate a supplied snapshot without reading the process environment or I/O."""
    if approved is not True:
        raise ValueError("TASK005_PROFILE_REJECTED")
    try:
        config = load_database_config(environment)
    except ConfigError:
        raise ValueError("TASK005_PROFILE_REJECTED") from None
    if (type(approved_database) is not str or type(approved_test_role) is not str or type(preparation_role) is not str
            or type(excluded_resources) is not frozenset
            or any(type(name) is not str for name in excluded_resources)):
        raise ValueError("TASK005_PROFILE_REJECTED")
    if (not re.fullmatch(r"task005_[a-z0-9_]+", config.name, re.ASCII)
            or config.name != approved_database or config.user != approved_test_role
            or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", preparation_role, re.ASCII)
            or config.user == preparation_role
            or config.name in excluded_resources or config.user in excluded_resources
            or preparation_role in excluded_resources
            or (config.host, config.port, config.pool_size, config.max_overflow,
                config.pool_timeout, config.connect_timeout, config.pool_pre_ping,
                config.pool_recycle, config.isolation_level)
            != ("127.0.0.1", 5432, 2, 1, 5, 3, True, 1800, "READ COMMITTED")):
        raise ValueError("TASK005_PROFILE_REJECTED")
    return config
