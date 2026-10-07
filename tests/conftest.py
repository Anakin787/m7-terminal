"""Firestore emulator wiring, shared by every test that touches Store.

Requires ``firebase-tools`` (``npm install -g firebase-tools``) and a JRE on
PATH. Tests that need Firestore are skipped, not failed, when the emulator
cannot be started - a missing local toolchain is not a code defect.
"""

import os
import shutil
import socket
import subprocess
import time

import pytest
import requests

EMULATOR_PROJECT = "m7-terminal-test"
EMULATOR_HOST = "127.0.0.1:8080"


def _port_open(host, port, timeout=0.5):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, int(port))) == 0


@pytest.fixture(autouse=True)
def _never_reach_real_firestore(monkeypatch):
    """Fail a test that builds a Firestore client with no emulator behind it.

    ``firestore.Client()`` with no emulator host silently uses the machine's
    credentials and the real project. A test that forgot the emulator fixture
    (or ran alone, before the session fixture set the host) then reads and
    writes production: on 2026-09-29 a ``--reconcile`` test settled five live
    orders with fake fills that way. Checked when the client is built, not at
    import, because the emulator host is set only once its fixture has run.
    """
    from google.cloud import firestore

    real_init = firestore.Client.__init__

    def guarded(self, *args, **kwargs):
        if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
            raise RuntimeError(
                "테스트가 에뮬레이터 없이 Firestore 클라이언트를 만들었습니다 — "
                "firestore_client fixture를 쓰세요. 실계정 DB에 붙을 뻔했습니다."
            )
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(firestore.Client, "__init__", guarded)


@pytest.fixture(scope="session")
def firestore_emulator():
    host, port = EMULATOR_HOST.split(":")

    if shutil.which("firebase") is None:
        pytest.skip("firebase-tools가 설치되어 있지 않습니다: npm install -g firebase-tools")

    process = subprocess.Popen(
        [
            "firebase",
            "emulators:start",
            "--only",
            "firestore",
            "--project",
            EMULATOR_PROJECT,
        ],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _port_open(host, port):
                break
            if process.poll() is not None:
                output = process.stdout.read()
                pytest.skip(f"Firestore 에뮬레이터 기동 실패 (JRE 설치 필요할 수 있음):\n{output}")
            time.sleep(0.5)
        else:
            process.terminate()
            pytest.skip("Firestore 에뮬레이터가 30초 안에 기동하지 않았습니다.")

        os.environ["FIRESTORE_EMULATOR_HOST"] = EMULATOR_HOST
        # So that code under test calling firestore.Client() with no explicit
        # project (main.py, trade.py, ...) lands in the same emulator project
        # the firestore_client fixture wipes and asserts against.
        os.environ["GOOGLE_CLOUD_PROJECT"] = EMULATOR_PROJECT
        yield EMULATOR_HOST
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        os.environ.pop("FIRESTORE_EMULATOR_HOST", None)
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)


@pytest.fixture
def firestore_client(firestore_emulator):
    """A Firestore client against the emulator, wiped clean before use."""
    from google.cloud import firestore

    requests.delete(
        f"http://{firestore_emulator}/emulator/v1/projects/{EMULATOR_PROJECT}/"
        "databases/(default)/documents",
        timeout=10,
    )
    return firestore.Client(project=EMULATOR_PROJECT)
