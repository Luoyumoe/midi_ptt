import threading
import time
import unittest
from xmlrpc.server import SimpleXMLRPCServer

from midi_ptt_controller.backends import BackendError, FlrigBackend, RigctldBackend, SerialBackend
from midi_ptt_controller.config import BackendConfig


class FakeSerial:
    def __init__(self):
        self.is_open = False
        self.rts = False
        self.dtr = False
        self.states = []

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        if name in {"rts", "dtr"} and getattr(self, "is_open", False):
            self.states.append((name, value))


class SerialTests(unittest.TestCase):
    def test_rts_active_low_and_release(self):
        port = FakeSerial()
        backend = SerialBackend(
            BackendConfig(kind="serial", serial_port="COM3", serial_line="RTS", serial_active_low=True),
            serial_factory=lambda: port,
        )
        backend.connect()
        self.assertTrue(port.rts)
        backend.set_ptt(True)
        self.assertFalse(port.rts)
        backend.set_ptt(False)
        self.assertTrue(port.rts)
        backend.set_ptt(True)
        backend.connect()
        self.assertTrue(port.rts)
        backend.close()
        self.assertFalse(port.is_open)


class _FakeSocket:
    def __init__(self, response=b"RPRT 0\n"):
        self.response = bytearray(response)
        self.commands = []
        self.timeout = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        self.commands.append(data)

    def recv(self, size):
        if not self.response:
            return b""
        result = self.response[:size]
        del self.response[:size]
        return bytes(result)


class RigctldTests(unittest.TestCase):
    def test_commands_are_protocol_checked(self):
        calls = []

        def factory(address, timeout):
            sock = _FakeSocket()
            calls.append((address, timeout, sock))
            return sock

        backend = RigctldBackend(
            BackendConfig(kind="rigctld", host="127.0.0.1", port=4532), socket_factory=factory
        )
        backend.connect()
        backend.set_ptt(True)
        backend.set_ptt(False)
        self.assertEqual([item[2].commands[0] for item in calls], [b"T 0\n", b"T 1\n", b"T 0\n"])

    def test_nonzero_rprt_is_error(self):
        backend = RigctldBackend(
            BackendConfig(kind="rigctld", host="localhost", port=4532),
            socket_factory=lambda *_args: _FakeSocket(b"RPRT -1\n"),
        )
        with self.assertRaises(BackendError):
            backend.connect()

    def test_socket_timeout_is_reported(self):
        backend = RigctldBackend(
            BackendConfig(kind="rigctld", host="localhost", port=4532),
            socket_factory=lambda *_args: (_ for _ in ()).throw(TimeoutError("timed out")),
        )
        with self.assertRaisesRegex(BackendError, "timed out"):
            backend.connect()


class FlrigTests(unittest.TestCase):
    def test_xmlrpc_calls_ptt_off_then_on_and_off(self):
        calls = []
        server = SimpleXMLRPCServer(("127.0.0.1", 0), logRequests=False, allow_none=True)
        server.register_function(lambda value: calls.append(value) or 0, "rig.set_ptt")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            backend = FlrigBackend(
                BackendConfig(kind="flrig", url=f"http://127.0.0.1:{server.server_address[1]}")
            )
            backend.connect()
            backend.set_ptt(True)
            backend.set_ptt(False)
            backend.close()
            self.assertEqual(calls, [0, 1, 0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_xmlrpc_timeout_is_enforced(self):
        server = SimpleXMLRPCServer(("127.0.0.1", 0), logRequests=False, allow_none=True)

        def delayed(_value):
            time.sleep(0.3)
            return 0

        server.register_function(delayed, "rig.set_ptt")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            backend = FlrigBackend(
                BackendConfig(
                    kind="flrig",
                    url=f"http://127.0.0.1:{server.server_address[1]}",
                    timeout_seconds=0.1,
                )
            )
            with self.assertRaises(BackendError):
                backend.connect()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
