"""dis-sim's --targets / DIS_TARGETS fan-out.

A real DIS deployment often puts every receiver on one shared multicast
group, so one entity's traffic reaches every listener. dis-sim speaks
unicast UDP, so --targets is the stand-in: fan every PDU out to a list of
host:port destinations instead of just --host/--port. These tests exercise
the parser, the single sendto chokepoint (send_pdu), and the startup
conflict check directly -- same convention as this directory's other
dis_sim.py unit tests (parse_destroy_schedule, parse_fire_schedule, etc.):
no process spawned, no network beyond two local loopback sockets bound to
ephemeral ports.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dis-sim"))

from dis_sim import (  # noqa: E402
    _DEFAULT_HOST,
    check_targets_host_conflict,
    parse_targets,
    send_pdu,
)


def _bound_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(1.0)
    return s


# ---------------------------------------------------------------------------
# parse_targets
# ---------------------------------------------------------------------------

def test_parse_targets_empty_is_no_fanout():
    assert parse_targets("") == []
    assert parse_targets("   ") == []


def test_parse_targets_parses_host_port_pairs():
    assert parse_targets("127.0.0.1:9001,127.0.0.1:9002") == [
        ("127.0.0.1", 9001), ("127.0.0.1", 9002),
    ]


def test_parse_targets_tolerates_whitespace_and_blank_entries():
    assert parse_targets(" 127.0.0.1:9001 , , 10.0.0.5:9002") == [
        ("127.0.0.1", 9001), ("10.0.0.5", 9002),
    ]


def test_parse_targets_rejects_missing_port():
    with pytest.raises(SystemExit, match="not host:port"):
        parse_targets("127.0.0.1")


def test_parse_targets_rejects_non_numeric_port():
    with pytest.raises(SystemExit, match="not host:port"):
        parse_targets("127.0.0.1:notaport")


def test_parse_targets_rejects_empty_host():
    with pytest.raises(SystemExit, match="not host:port"):
        parse_targets(":9001")


# ---------------------------------------------------------------------------
# check_targets_host_conflict
# ---------------------------------------------------------------------------

def test_conflict_check_allows_targets_with_default_host():
    check_targets_host_conflict(_DEFAULT_HOST, [("127.0.0.1", 9001)])  # no raise


def test_conflict_check_allows_non_default_host_without_targets():
    check_targets_host_conflict("10.0.0.5", [])  # no raise


def test_conflict_check_allows_no_targets_no_host_override():
    check_targets_host_conflict(_DEFAULT_HOST, [])  # no raise


def test_conflict_check_refuses_targets_with_non_default_host():
    with pytest.raises(SystemExit, match="cannot both be set"):
        check_targets_host_conflict("10.0.0.5", [("127.0.0.1", 9001)])


# ---------------------------------------------------------------------------
# send_pdu: the single sendto chokepoint
# ---------------------------------------------------------------------------

def test_send_pdu_unset_targets_sends_only_to_host_port():
    recv_a = _bound_socket()
    recv_b = _bound_socket()
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        payload = b"only-to-host"
        send_pdu(sender, payload, "127.0.0.1", recv_a.getsockname()[1], [])

        data, _ = recv_a.recvfrom(1024)
        assert data == payload

        with pytest.raises(socket.timeout):
            recv_b.recvfrom(1024)
    finally:
        sender.close()
        recv_a.close()
        recv_b.close()


def test_send_pdu_with_targets_fans_out_identical_bytes_to_every_target():
    recv_a = _bound_socket()
    recv_b = _bound_socket()
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        payload = b"fan-out-bytes"
        targets = [
            ("127.0.0.1", recv_a.getsockname()[1]),
            ("127.0.0.1", recv_b.getsockname()[1]),
        ]
        # --host/--port are irrelevant once targets is non-empty -- pass
        # obviously-wrong values to prove they are never consulted.
        send_pdu(sender, payload, "0.0.0.0", 1, targets)

        data_a, _ = recv_a.recvfrom(1024)
        data_b, _ = recv_b.recvfrom(1024)
        assert data_a == payload
        assert data_b == payload
    finally:
        sender.close()
        recv_a.close()
        recv_b.close()


def test_send_pdu_one_failing_target_does_not_silence_the_others():
    """A send error on the first target still lets the second receive the
    PDU, and the error is still raised so the caller counts it."""

    class FlakySock:
        def __init__(self):
            self.sent = []

        def sendto(self, payload, dest):
            if dest[0] == "bad":
                raise OSError("unreachable")
            self.sent.append((payload, dest))

    sock = FlakySock()
    with pytest.raises(OSError, match="unreachable"):
        send_pdu(sock, b"pdu", "127.0.0.1", 1,
                         [("bad", 1), ("good", 2)])
    assert sock.sent == [(b"pdu", ("good", 2))]
