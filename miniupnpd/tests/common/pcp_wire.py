"""PCP (RFC 6887) and NAT-PMP (RFC 6886) names and packet encoding shared by the test tools."""

import ipaddress
import socket
import struct

PCP_PORT = 5351

PCP_OPCODES = {"ANNOUNCE": 0, "MAP": 1, "PEER": 2, "AUTHENTICATION": 3}
NATPMP_OPCODES = {"PUBLIC_ADDRESS": 0, "MAP_UDP": 1, "MAP_TCP": 2}
PROTOCOLS = {"UDP": 17, "TCP": 6}
OPTIONS = {"THIRD_PARTY": 1, "PREFER_FAILURE": 2, "FILTER": 3, "NONCE": 4, "PORT_SET": 130}
OPTION_UNKNOWN_OPTIONAL = 128   # an unassigned code in the optional range (>= 128)
RESULTS = {
    "pcp": {"SUCCESS": 0, "UNSUPP_VERSION": 1, "NOT_AUTHORIZED": 2, "MALFORMED_REQUEST": 3,
            "UNSUPP_OPCODE": 4, "UNSUPP_OPTION": 5, "MALFORMED_OPTION": 6, "NETWORK_FAILURE": 7,
            "NO_RESOURCES": 8, "UNSUPP_PROTOCOL": 9, "USER_EX_QUOTA": 10,
            "CANNOT_PROVIDE_EXTERNAL": 11, "ADDRESS_MISMATCH": 12, "EXCESSIVE_REMOTE_PEERS": 13},
    "natpmp": {"SUCCESS": 0, "UNSUPP_VERSION": 1, "NOT_AUTHORIZED": 2, "NETWORK_FAILURE": 3,
               "OUT_OF_RESOURCES": 4, "UNSUPP_OPCODE": 5},
}
RESPONSE_OPCODES = {
    "pcp": {name + "_RESPONSE": 128 + code for name, code in PCP_OPCODES.items()},
    "natpmp": {name + "_RESPONSE": 128 + code for name, code in NATPMP_OPCODES.items()},
}


def names_by_code(table):
    """Invert a name -> code table."""
    return {code: name for name, code in table.items()}


def ipv6_bytes(value):
    """16-byte form of an IPv4 (as ::ffff:a.b.c.d) or IPv6 address string."""
    address = ipaddress.ip_address(value)
    if address.version == 4:
        return socket.inet_pton(socket.AF_INET6, f"::ffff:{address}")
    return address.packed


def pcp_header(version, opcode, lifetime, client, reserved=0):
    """PCP common request header; `client` is an address string or 16 raw bytes."""
    if isinstance(client, str):
        client = ipv6_bytes(client)
    return struct.pack("!BBHI", version, opcode, reserved, lifetime) + client


def pcp_map_body(protocol, internal_port, external_port, external_address=b"\0" * 16, nonce=None):
    """MAP opcode body; PCP version 1 (draft) has no nonce."""
    return ((nonce or b"") + bytes((protocol, 0, 0, 0)) + struct.pack("!HH", internal_port, external_port)
            + external_address)


def pcp_peer_tail(remote_port, remote_address, reserved=0):
    """The PEER-specific fields that follow a MAP body."""
    if isinstance(remote_address, str):
        remote_address = ipv6_bytes(remote_address)
    return struct.pack("!HH", remote_port, reserved) + remote_address


def pcp_option(code, data=b""):
    """One option, padded to a 4-octet boundary."""
    return struct.pack("!BBH", code, 0, len(data)) + data + b"\0" * (-len(data) % 4)


def pcp_filter_option(prefix_length, remote_port, remote_address):
    """FILTER option; `prefix_length` is the on-the-wire (IPv6-mapped) prefix length."""
    if isinstance(remote_address, str):
        remote_address = ipv6_bytes(remote_address)
    return pcp_option(OPTIONS["FILTER"], bytes((0, prefix_length)) + struct.pack("!H", remote_port)
                      + remote_address)


def natpmp_packet(opcode, private_port=0, public_port=0, lifetime=0, version=0, reserved=0):
    """NAT-PMP mapping request (or the same layout with another opcode)."""
    return struct.pack("!BBHHHI", version, opcode, reserved, private_port, public_port, lifetime)
