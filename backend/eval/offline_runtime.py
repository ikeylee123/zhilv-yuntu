"""Evaluation-only isolation. Run in a fresh process before storage imports."""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import patch


@contextmanager
def offline_runtime(*, allow_loopback=False):
    attempts = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    def deny(*args, **kwargs):
        caller = sys._getframe(1)
        # Windows implements stdlib socketpair via a private loopback connection.
        # Do not allow arbitrary localhost providers or Redis connections.
        if caller.f_globals.get("__name__") == "socket" and caller.f_code.co_name == "_fallback_socketpair":
            return original_connect(*args, **kwargs)
        address = args[1] if len(args) > 1 else None
        if allow_loopback and isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost"):
            return original_connect(*args, **kwargs)
        attempts.append("blocked network connection")
        raise RuntimeError("Offline evaluation forbids network connections")
    with tempfile.TemporaryDirectory(prefix="zhilv-offline-") as folder, ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "LLM_API_KEY": "", "AMAP_API_KEY": "", "REDIS_ENABLED": "false",
            "ENABLE_AMAP_ENRICHMENT": "false", "CHROMA_DB_DIR": str(Path(folder)/"chroma"),
        }))
        stack.enter_context(patch.object(socket.socket, "connect", deny))
        def deny_ex(connection, address):
            if allow_loopback and address[0] in ("127.0.0.1", "::1", "localhost"):
                return original_connect_ex(connection, address)
            return deny(connection, address)
        stack.enter_context(patch.object(socket.socket, "connect_ex", deny_ex))
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import app.config as config
        engine = create_engine("sqlite:///" + str(Path(folder)/"eval.db"), connect_args={"check_same_thread": False})
        stack.enter_context(patch.object(config, "engine", engine))
        stack.enter_context(patch.object(config, "SessionLocal", sessionmaker(bind=engine)))
        from app.services import storage_service as store
        stack.enter_context(patch.object(store, "engine", engine))
        stack.enter_context(patch.object(store, "SessionLocal", config.SessionLocal))
        store.init_db()
        try:
            yield {"folder": folder, "network_attempts": attempts}
        finally:
            engine.dispose()
