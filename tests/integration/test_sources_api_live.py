"""Opt-in HTTP + PostgreSQL tests for sources CRUD; no external target I/O.

The fixture checks the explicitly authorised TASK-005 profile before access.
The application itself performs the real HTTP/service/UoW/repository work.
"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import importlib.util
import json
import logging
import os
from pathlib import Path
from threading import Barrier, Event, Lock
from uuid import UUID, uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from dataflow_manager.security.crypto import CryptoError, CryptoErrorCode, CryptoService


_spec = importlib.util.spec_from_file_location("_sources_http_gate", Path(__file__).with_name("conftest.py"))
_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gate)


def _record(config, event, *, name=None, identifier=None):
    """Record intended mutations before HTTP and outcomes without secrets."""
    if event not in {"create_intended", "created", "rename_intended", "delete_intended", "deleted",
                     "update_intended", "updated"}:
        raise RuntimeError("CRUD_LEDGER_REJECTED")
    if config.name != os.environ.get("DFM_TEST_APPROVED_DATABASE"):
        raise RuntimeError("CRUD_LEDGER_REJECTED")
    if name is not None and (not name.startswith("crud_") or len(name) > 90):
        raise RuntimeError("CRUD_LEDGER_REJECTED")
    if identifier is not None:
        parsed = UUID(str(identifier))
        if parsed.version != 4 or str(parsed) != str(identifier):
            raise RuntimeError("CRUD_LEDGER_REJECTED")
    payload = {"database": config.name, "kind": "http_crud", "event": event}
    if name is not None:
        payload["name"] = name
    if identifier is not None:
        payload["source_id"] = str(identifier)
    data = (json.dumps(payload, ensure_ascii=True) + "\n").encode()
    fd = os.open(_gate.ledger_path(os.environ), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        if os.write(fd, data) != len(data):
            raise RuntimeError("CRUD_LEDGER_WRITE_FAILED")
    finally:
        os.close(fd)


def _input(name, password="synthetic-long-password-111"):
    return {"name": name, "host": "example.test", "port": 5432,
            "database": "source_db", "username": "source_user", "password": password}


def _new_name():
    return "crud_" + uuid4().hex


def _create(client, config, name, password="synthetic-long-password-111"):
    _record(config, "create_intended", name=name)
    response = client.post("/api/v1/sources", json=_input(name, password))
    if response.status_code == 201:
        _record(config, "created", name=name, identifier=response.json()["id"])
    return response


def _get_token(engine, identifier):
    with engine.connect() as connection:
        return connection.execute(text("SELECT password_encrypted FROM public.data_sources WHERE id=:id"),
                                  {"id": UUID(identifier)}).scalar_one()


def _no_secrets(response, *values):
    raw = response.content
    return all(value.encode() not in raw for value in values)


def test_http_crud_crypto_and_independent_units_of_work(task005_real_integration, monkeypatch, caplog):
    config, engine, _ = task005_real_integration
    from dataflow_manager.main import app

    key = Fernet.generate_key()
    password = "synthetic-http-credential-one"
    replacement = "synthetic-http-credential-two"
    name = _new_name()
    monkeypatch.setenv("DATAFLOW_FERNET_KEY", key.decode("ascii"))
    caplog.set_level(logging.WARNING)
    logging.getLogger("crud-positive-control").warning("%s", password)
    positive_control_observed = password in caplog.text
    assert positive_control_observed
    caplog.clear()
    sessions = []
    sessions_lock = Lock()
    with TestClient(app) as client:
        resources = app.state._dfm_db_resources
        assert resources is not None
        def observed_factory():
            session = resources[1]()
            with sessions_lock:
                sessions.append(session)
            return session
        app.state._dfm_db_resources = (resources[0], observed_factory)

        created = _create(client, config, name, password)
        assert created.status_code == 201
        data = created.json()
        identifier = data["id"]
        assert created.headers["location"] == "/api/v1/sources/" + identifier
        assert set(data) == {"id", "name", "host", "port", "database", "username", "created_at", "updated_at"}
        token = _get_token(engine, identifier)
        token_is_ciphertext = token != password and password not in token
        assert token_is_ciphertext
        original_decrypts = Fernet(key).decrypt(token.encode()) == password.encode()
        assert original_decrypts
        response_safe = _no_secrets(created, password, replacement, key.decode(), token)
        assert response_safe

        updated = client.patch("/api/v1/sources/" + identifier, json={"host": "new.example.test"})
        assert updated.status_code == 200
        unchanged_token = _get_token(engine, identifier) == token
        assert unchanged_token
        replaced = client.patch("/api/v1/sources/" + identifier, json={"password": replacement})
        assert replaced.status_code == 200
        new_token = _get_token(engine, identifier)
        token_changed = new_token != token
        assert token_changed
        replacement_decrypts = Fernet(key).decrypt(new_token.encode()) == replacement.encode()
        assert replacement_decrypts
        update_safe = _no_secrets(updated, password, replacement, key.decode(), token, new_token)
        replacement_safe = _no_secrets(replaced, password, replacement, key.decode(), token, new_token)
        assert update_safe and replacement_safe

        assert client.get("/api/v1/sources/" + identifier).status_code == 200
        listed = client.get("/api/v1/sources")
        assert listed.status_code == 200 and any(item["id"] == identifier for item in listed.json())
        list_safe = _no_secrets(listed, password, replacement, key.decode(), token, new_token)
        assert list_safe

        bad = client.post("/api/v1/sources", json=dict(_input(_new_name(), password), port=True,
                                                        extra_secret=password))
        validation_safe = _no_secrets(bad, password, key.decode())
        assert bad.status_code == 422 and validation_safe
        _record(config, "delete_intended", identifier=identifier)
        deleted = client.delete("/api/v1/sources/" + identifier)
        if deleted.status_code == 204:
            _record(config, "deleted", identifier=identifier)
        assert deleted.status_code == 204 and deleted.content == b""
        assert client.get("/api/v1/sources/" + identifier).status_code == 404
        assert len(sessions) >= 7 and len({id(session) for session in sessions}) == len(sessions)
        assert all(not session.in_transaction() for session in sessions)

    assert app.state._dfm_db_resources is None
    assert app.state._dfm_crypto is None
    logs_safe = all(value not in caplog.text for value in (password, replacement, key.decode(), token, new_token))
    assert logs_safe


def _race(actions):
    barrier = Barrier(2, timeout=10)
    def run(action):
        barrier.wait()
        return action()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, action) for action in actions]
        return [future.result(timeout=30) for future in futures]


def test_parallel_http_create_and_rename_keep_unique_names(task005_real_integration, monkeypatch):
    config, _, _ = task005_real_integration
    from dataflow_manager.main import app

    monkeypatch.setenv("DATAFLOW_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    with TestClient(app) as client:
        name = _new_name()
        # The first casefold form remains the ledger key for the second request.
        _record(config, "create_intended", name=name)
        _record(config, "create_intended", name=name)
        results = _race([
            lambda: client.post("/api/v1/sources", json=_input(name)),
            lambda: client.post("/api/v1/sources", json=_input(name.upper())),
        ])
        for response in results:
            if response.status_code == 201:
                _record(config, "created", name=name, identifier=response.json()["id"])
        assert sorted(response.status_code for response in results) == [201, 409]

        source_a = _create(client, config, _new_name())
        source_b = _create(client, config, _new_name())
        assert source_a.status_code == source_b.status_code == 201
        ids = [source_a.json()["id"], source_b.json()["id"]]
        target = _new_name()
        for identifier in ids:
            _record(config, "rename_intended", name=target, identifier=identifier)
        renamed = _race([
            lambda: client.patch("/api/v1/sources/" + ids[0], json={"name": target}),
            lambda: client.patch("/api/v1/sources/" + ids[1], json={"name": target.upper()}),
        ])
        assert sorted(response.status_code for response in renamed) == [200, 409]
        listed = client.get("/api/v1/sources")
        assert listed.status_code == 200
        assert sum(item["name"].casefold() == target.casefold() for item in listed.json()) == 1


def test_http_conflicting_patch_rolls_back_password_change(task005_real_integration, monkeypatch):
    config, engine, _ = task005_real_integration
    from dataflow_manager.main import app

    monkeypatch.setenv("DATAFLOW_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    with TestClient(app) as client:
        first_name, second_name = _new_name(), _new_name()
        first = _create(client, config, first_name)
        second = _create(client, config, second_name)
        assert first.status_code == second.status_code == 201
        identifier = second.json()["id"]
        original = _get_token(engine, identifier)
        _record(config, "rename_intended", name=first_name, identifier=identifier)
        failed = client.patch("/api/v1/sources/" + identifier,
                              json={"name": first_name, "password": "synthetic-conflict-password"})
        assert failed.status_code == 409
        token_unchanged = _get_token(engine, identifier) == original
        assert token_unchanged
        loaded = client.get("/api/v1/sources/" + identifier)
        assert loaded.status_code == 200 and loaded.json()["name"] == second_name
        response_safe = _no_secrets(failed, "synthetic-conflict-password", original)
        assert response_safe


def test_patch_not_found_precedes_crypto_and_failed_crypto_releases_lock(task005_real_integration, monkeypatch):
    config, engine, _ = task005_real_integration
    from dataflow_manager.main import app

    monkeypatch.setenv("DATAFLOW_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    with TestClient(app) as client:
        created = _create(client, config, _new_name())
        assert created.status_code == 201
        identifier = created.json()["id"]
        original = _get_token(engine, identifier)

        class ForbiddenCrypto:
            def encrypt(self, _):
                raise AssertionError("crypto invoked for missing row")
        app.state._dfm_crypto = ForbiddenCrypto()
        missing = client.patch("/api/v1/sources/" + str(uuid4()), json={"password": "synthetic-new"})
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "source_not_found"
        app.state._dfm_crypto = None
        unavailable = client.patch("/api/v1/sources/" + identifier, json={"password": "synthetic-new"})
        assert unavailable.status_code == 503

        locked, release = Event(), Event()
        class FailingCrypto:
            def encrypt(self, _):
                locked.set()
                if not release.wait(8):
                    raise RuntimeError("synthetic crypto wait timeout")
                raise CryptoError(CryptoErrorCode.ENCRYPTION_FAILED)
        app.state._dfm_crypto = FailingCrypto()
        _record(config, "update_intended", identifier=identifier)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.patch, "/api/v1/sources/" + identifier,
                                 json={"password": "synthetic-new"})
            try:
                assert locked.wait(8)
                with engine.connect() as connection:
                    try:
                        connection.execute(text("SELECT id FROM public.data_sources WHERE id=:id FOR UPDATE NOWAIT"),
                                           {"id": UUID(identifier)})
                    except DBAPIError as error:
                        assert getattr(error.orig, "sqlstate", None) == "55P03"
                    else:
                        raise AssertionError("PATCH did not hold the row lock during encryption")
            finally:
                release.set()
            response = future.result(timeout=12)
        assert response.status_code == 500
        assert _get_token(engine, identifier) == original
        with engine.connect() as connection:
            row = connection.execute(text("SELECT id FROM public.data_sources WHERE id=:id FOR UPDATE NOWAIT"),
                                     {"id": UUID(identifier)}).scalar_one()
            assert str(row) == identifier


def test_patch_holds_row_lock_until_commit_against_concurrent_delete(task005_real_integration, monkeypatch):
    config, engine, _ = task005_real_integration
    from dataflow_manager.main import app

    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("DATAFLOW_FERNET_KEY", key)
    with TestClient(app) as client:
        created = _create(client, config, _new_name())
        assert created.status_code == 201
        identifier = created.json()["id"]
        locked, release, delete_started = Event(), Event(), Event()
        actual_crypto = CryptoService(key)
        class BlockingCrypto:
            def encrypt(self, password):
                locked.set()
                if not release.wait(8):
                    raise RuntimeError("synthetic crypto wait timeout")
                return actual_crypto.encrypt(password)
        app.state._dfm_crypto = BlockingCrypto()
        _record(config, "update_intended", identifier=identifier)
        _record(config, "delete_intended", identifier=identifier)
        def delete():
            delete_started.set()
            return client.delete("/api/v1/sources/" + identifier)
        with ThreadPoolExecutor(max_workers=2) as pool:
            patch = pool.submit(client.patch, "/api/v1/sources/" + identifier,
                                json={"password": "synthetic-new"})
            try:
                assert locked.wait(8)
                with engine.connect() as connection:
                    try:
                        connection.execute(text("SELECT id FROM public.data_sources WHERE id=:id FOR UPDATE NOWAIT"),
                                           {"id": UUID(identifier)})
                    except DBAPIError as error:
                        assert getattr(error.orig, "sqlstate", None) == "55P03"
                    else:
                        raise AssertionError("PATCH did not hold the row lock during encryption")
                deletion = pool.submit(delete)
                assert delete_started.wait(8)
                try:
                    deletion.result(timeout=0.25)
                except FutureTimeout:
                    pass
                else:
                    raise AssertionError("concurrent delete did not wait for PATCH")
            finally:
                release.set()
            updated = patch.result(timeout=12)
            deleted = deletion.result(timeout=12)
        if updated.status_code == 200:
            _record(config, "updated", identifier=identifier)
        if deleted.status_code == 204:
            _record(config, "deleted", identifier=identifier)
        assert updated.status_code == 200 and deleted.status_code == 204
        assert client.get("/api/v1/sources/" + identifier).status_code == 404
