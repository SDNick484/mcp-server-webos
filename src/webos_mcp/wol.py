"""Wake-on-LAN: the only way to turn on a webOS TV (ASSUMPTION W-TURNON-DEAD).

Once a TV is off, its WebSocket is gone, so nothing over SSAP can wake it.
A magic packet (six 0xFF bytes, then the MAC sixteen times) sent over UDP
wakes it, provided its "TV On With Mobile" / "Mobile TV On" / "Turn on via
Wi-Fi" setting is on (ASSUMPTION W-WOL; the name varies by generation).

Each packet goes two ways, an idea from lgtv-mcp: to the broadcast address
(the classic route, which only reaches the TVs' own network segment) and
straight to the TV's last known address (which some switches and Wi-Fi
access points forward when they drop broadcasts). From a Proxmox LXC the
broadcast only reaches the TVs if the container is bridged onto the same
network; doctor shows which local address the TV is reached from.
"""

from __future__ import annotations

import socket

from .config import normalize_mac


def magic_packet(mac: str) -> bytes:
    normalized = normalize_mac(mac)
    if normalized is None:
        raise ValueError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(normalized.replace(":", "")) * 16


def targets(host: str | None, broadcast: str, port: int) -> list[tuple[str, int]]:
    out = [(broadcast, port)]
    if host and host != broadcast:
        out.append((host, port))
    return out


def send(macs: tuple[str, ...] | list[str], host: str | None, broadcast: str, port: int) -> list[str]:
    """Send a packet for each MAC to each target. Returns what was sent, for
    ActionResult.sent. Fails only if every send failed."""
    sent: list[str] = []
    failure: OSError | None = None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for mac in macs:
            packet = magic_packet(mac)
            for addr in targets(host, broadcast, port):
                try:
                    sock.sendto(packet, addr)
                    sent.append(f"wol {mac} -> {addr[0]}:{addr[1]}")
                except OSError as exc:  # e.g. no broadcast route in this network namespace
                    failure = exc
    if not sent and failure is not None:
        raise failure
    return sent


def route_source(host: str) -> str | None:
    """The local address this machine uses to reach host (no packet is sent:
    connecting a UDP socket only consults the routing table)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((host, 9))
            return str(sock.getsockname()[0])
    except OSError:
        return None
