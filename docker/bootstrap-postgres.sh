#!/usr/bin/env bash
set +x
set +v
set -euo pipefail
umask 077

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    printf '%s\n' 'STOP: bootstrap must be executable, not sourced' >&2
    return 1
fi

stage=preflight
readonly data_dir=/var/lib/postgresql/18/docker
readonly attempt_marker=/var/lib/postgresql/.dataflow-init-started
readonly secret_dir=/run/dataflow-bootstrap/secrets
readonly psql_bin=/usr/lib/postgresql/18/bin/psql

on_exit() {
    local rc=$?
    trap - EXIT
    unset PGPASSWORD DATAFLOW_APP_PASSWORD DATAFLOW_TEST_PASSWORD PGOPTIONS
    if (( rc != 0 )); then
        printf 'STOP stage=%s exit=%s; user decision required; no automatic retry or cleanup\n' \
            "$stage" "$rc" >&2
    fi
    exit "$rc"
}
trap on_exit EXIT

if [[ "$(id -un)" != postgres || "${PGDATA:-}" != "$data_dir" ]]; then
    printf '%s\n' 'STOP: unexpected user or PGDATA' >&2
    exit 1
fi
if (( $# != 0 )); then
    printf '%s\n' 'STOP: unexpected arguments' >&2
    exit 1
fi

if [[ ! -f "$attempt_marker" ]]; then
    printf '%s\n' 'STOP: initialization attempt marker is missing' >&2
    exit 1
fi

if [[ -e "$data_dir/.dataflow-bootstrap-complete" ]]; then
    printf '%s\n' 'STOP: bootstrap already completed' >&2
    exit 1
fi

# Remove inherited libpq settings before constructing explicit connections.
for variable in "${!PG@}"; do
    unset "$variable"
done

read_secret() {
    local name=$1 target=$2 value
    stage="read-secret:$name"
    if [[ ! -f "$secret_dir/$name" || -L "$secret_dir/$name" ]]; then
        printf 'STOP: missing or invalid secret file: %s\n' "$name" >&2
        exit 1
    fi
    value=$(cat "$secret_dir/$name")
    if [[ ! "$value" =~ ^[0-9a-f]{64}$ ]]; then
        printf 'STOP: invalid secret format: %s\n' "$name" >&2
        exit 1
    fi
    printf -v "$target" '%s' "$value"
    export "$target"
}

read_secret postgres_admin_password PGPASSWORD
read_secret dataflow_app_password DATAFLOW_APP_PASSWORD
read_secret dataflow_test_password DATAFLOW_TEST_PASSWORD

export PGCONNECT_TIMEOUT=5
export PGOPTIONS="-c statement_timeout=60000 -c lock_timeout=5000"
psql_args=(
    -X -q --no-password
    --host=/var/run/postgresql --port=5432
    --username=postgres --dbname=postgres
    --set=ON_ERROR_STOP=1
    --set=VERBOSITY=sqlstate
    --set=SHOW_CONTEXT=never
    --set=ECHO=none
    --set=ECHO_HIDDEN=off
    --file=-
)

# These probes check only psql rejection/exit semantics.
# They do not replace tests of actual target-role/database conflicts.
for probe in version role database; do
    stage="rejection-mechanism:$probe"
    printf 'stage=%s expected_sqlstate=22012\n' "$stage"
    if probe_output=$(
        PGOPTIONS="$PGOPTIONS -c default_transaction_read_only=on" \
        "$psql_bin" "${psql_args[@]}" --set=probe="$probe" <<'SQL'
SELECT 1 / CASE WHEN
    (:'probe' = 'version'
        AND current_setting('server_version_num')::integer = 0)
    OR (:'probe' = 'role'
        AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres'))
    OR (:'probe' = 'database'
        AND NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'postgres'))
THEN 1 ELSE 0 END;
\echo UNREACHABLE_AFTER_REJECTION
SQL
    ); then
        probe_rc=0
    else
        probe_rc=$?
    fi
    printf 'stage=%s actual_exit=%s\n' "$stage" "$probe_rc"
    if [[ "$probe_rc" -ne 3 || "$probe_output" == *UNREACHABLE_AFTER_REJECTION* ]]; then
        printf '%s\n' 'STOP: rejection mechanism probe failed' >&2
        exit 1
    fi
done

stage=sql-bootstrap
"$psql_bin" "${psql_args[@]}" <<'SQL'
\echo stage=validate-preconditions
SELECT 1 / CASE WHEN
    current_setting('server_version_num')::integer = 180006
    AND NOT EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname IN ('dataflow_app', 'dataflow_test')
    )
    AND NOT EXISTS (
        SELECT 1 FROM pg_database
        WHERE datname IN ('dataflow', 'dataflow_test')
    )
    AND NOT EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolcanlogin AND rolname <> 'postgres'
    )
THEN 1 ELSE 0 END AS preflight_ok;

\echo stage=preconditions-passed
\getenv app_password DATAFLOW_APP_PASSWORD
\getenv test_password DATAFLOW_TEST_PASSWORD

\echo stage=create-roles
BEGIN;
SET LOCAL log_statement = 'none';
SET LOCAL log_min_duration_statement = -1;
SET LOCAL log_min_duration_sample = -1;
SET LOCAL log_transaction_sample_rate = 0;
-- Suppress the failing statement text, which may contain a password.
-- Server error messages remain controlled by log_min_messages.
-- This setting is transaction-local.
SET LOCAL log_min_error_statement = 'panic';

\echo stage=create-role:dataflow_app
CREATE ROLE dataflow_app NOLOGIN PASSWORD :'app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\echo stage=create-role:dataflow_test
CREATE ROLE dataflow_test NOLOGIN PASSWORD :'test_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
COMMIT;
\unset app_password
\unset test_password

\echo stage=create-service-database
CREATE DATABASE dataflow OWNER dataflow_app ALLOW_CONNECTIONS false;

\echo stage=create-test-database
CREATE DATABASE dataflow_test OWNER dataflow_test ALLOW_CONNECTIONS false;

\echo stage=database-access
BEGIN;
REVOKE ALL ON DATABASE dataflow FROM PUBLIC;
REVOKE ALL ON DATABASE dataflow_test FROM PUBLIC;
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;
ALTER DATABASE dataflow ALLOW_CONNECTIONS true;
ALTER DATABASE dataflow_test ALLOW_CONNECTIONS true;
COMMIT;

\echo stage=service-schema
\connect dataflow
BEGIN;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO dataflow_app;
COMMIT;

\echo stage=test-schema
\connect dataflow_test
BEGIN;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO dataflow_test;
COMMIT;

\echo stage=enable-logins
\connect postgres
BEGIN;
ALTER ROLE dataflow_app LOGIN;
ALTER ROLE dataflow_test LOGIN;
COMMIT;
\echo stage=sql-complete
SQL

stage=write-completion-marker
(
    set -o noclobber
    printf '%s\n' 'postgresql=18.6 bootstrap=complete' \
        > "$data_dir/.dataflow-bootstrap-complete"
)
stage=complete
printf '%s\n' 'stage=complete'
