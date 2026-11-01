#!/usr/bin/env python3
"""Small UDP smoke-test harness for the miniupnpd PCP and NAT-PMP server.

Run against a configured, running miniupnpd instance. Mapping lifecycle cases run by default; use a disposable test gateway.
Pass --no-mutating to skip mapping changes.
"""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone

TIMEOUT = 2.0


FILTERED_SKIP_REASONS = frozenset({
    "excluded by --profile filter",
    "excluded by --protocol filter",
    "excluded by --family filter",
    "not selected by --case",
    "not selected for this invocation",
})


def is_filtered_skip(status, detail):
    return status == "SKIP" and detail in FILTERED_SKIP_REASONS


def capture_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise argparse.ArgumentTypeError(
            "capture name must start with a letter or digit and contain only letters, digits, _, - or .")
    return value


def prepare_capture_directory(args):
    if not args.capture_dir:
        return
    names = set(args.case or [])
    label = args.capture_name or (next(iter(names)) if len(names) == 1 else "all")
    # Docker supplies one timestamp for its IPv4 and IPv6 invocations.
    timestamp = os.environ.get("PCP_TEST_CAPTURE_TIMESTAMP")
    if not timestamp:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S.%f")
    capture_name(timestamp)
    label = capture_name(label)
    args.capture_dir = os.path.join(args.capture_dir, f"{timestamp}_{label}")
    os.makedirs(args.capture_dir, exist_ok=True)
    print(f"Capture directory: {args.capture_dir}")


def start_packet_capture(args, case):
    if not args.capture_dir:
        return None
    case_name = case["name"].replace("/", "_")
    filename = f"{args.family}-{case['protocol']}-{case_name}"
    pcap_path = os.path.join(args.capture_dir, filename + ".pcap")
    log_path = os.path.join(args.capture_dir, filename + ".tcpdump.log")
    command = [args.tcpdump, "--immediate-mode", "-i", args.capture_interface,
               "-nn", "-s0", "-U", "-w", pcap_path,
               case.get("capture_filter", f"udp port {args.port}")]
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True)
    except OSError as error:
        raise SystemExit(f"unable to start tcpdump: {error}") from error
    ready_line = process.stderr.readline()
    if "listening on" not in ready_line:
        details = ready_line + process.stderr.read()
        raise SystemExit(f"tcpdump failed to start for case {case_name}: {details.strip()}")
    return process, pcap_path, log_path, args.capture_timeout, ready_line


def stop_packet_capture(capture):
    if capture is None:
        return
    process, pcap_path, log_path, capture_timeout, ready_line = capture
    try:
        process.send_signal(signal.SIGINT)
        _, details = process.communicate(timeout=capture_timeout)
    except subprocess.TimeoutExpired:
        process.send_signal(signal.SIGINT)
        try:
            _, details = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            _, details = process.communicate()
    with open(log_path, "w", encoding="utf-8") as log_file:
        log_file.write(ready_line + details)
    print(f"PCAP {pcap_path}")


def assert_response_echo(request, response, expectation):
    for region in expectation.get("echo_request", []):
        start = region["offset"]
        end = start + region["length"]
        assert 0 <= start <= end <= len(request), "invalid request echo region"
        assert len(response) >= end, "response truncates echoed request bytes"
        assert response[start:end] == request[start:end], f"request bytes {start}:{end} were not echoed"


def markdown_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


PLATFORM_ORDER = ("linux", "freebsd")
STATUS_ORDER = ("PASS", "XFAIL", "FAIL", "SKIP")


def status_data_path(path):
    """Per-platform results behind the combined report: status.md -> status-data.json."""
    return os.path.splitext(path)[0] + "-data.json"


def load_status_data(path):
    try:
        with open(path, encoding="utf-8") as data_file:
            data = json.load(data_file)
        if data.get("version") == 1 and isinstance(data.get("platforms"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "platforms": {}}


def write_status_report(path, args, sections, outcomes):
    """Merge this run into the per-platform results and rewrite the combined report.

    Every platform (Linux in Docker, FreeBSD in a VM, ...) fills its own column; a run
    replaces only the cases and address family it actually exercised.
    """
    platform = args.platform
    all_cases = [case for section in sections for case in section["cases"]]
    known = {f"{case['protocol']}/{case['name']}" for case in all_cases}
    data_path = getattr(args, "status_data_file", None) or status_data_path(path)
    data = load_status_data(data_path)
    entry = data["platforms"].setdefault(platform, {"results": {}})
    entry["updated"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    entry.setdefault("runs", {})[args.family] = {
        "target": f"{args.host}:{args.port}", "protocol": args.protocol,
        "profile": getattr(args, "profile", "baseline"),
        "mutating": bool(args.mutating),
    }
    results = entry["results"].setdefault(args.family, {})
    for case in all_cases:
        status, detail = outcomes[(case["protocol"], case["name"])]
        key = f"{case['protocol']}/{case['name']}"
        if is_filtered_skip(status, detail):
            continue
        # A run without the fixture must not wipe the result of one that had it.
        if status == "SKIP" and detail.startswith("requires daemon-restart fixture") and key in results:
            continue
        results[key] = [status, detail]
    for other in data["platforms"].values():  # forget cases that left the corpus
        for family_results in other["results"].values():
            for key in [key for key in family_results if key not in known]:
                del family_results[key]

    platforms = sorted(data["platforms"], key=lambda name: (
        PLATFORM_ORDER.index(name) if name in PLATFORM_ORDER else len(PLATFORM_ORDER), name))
    lines = [
        "# PCP/NAT-PMP Test Status",
        "",
        "One column per platform; a run updates only its own platform's column "
        "(raw results: `" + os.path.basename(data_path) + "`).",
        "",
        "## Platforms",
        "",
    ]
    for name in platforms:
        counts = dict.fromkeys(STATUS_ORDER, 0)
        for family_results in data["platforms"][name]["results"].values():
            for status, _detail in family_results.values():
                counts[status] += 1
        runs = data["platforms"][name].get("runs", {})
        lines.append(
            f"- **{name}**: **{counts['PASS']} PASS**, **{counts['XFAIL']} XFAIL**, "
            f"**{counts['FAIL']} FAIL**, **{counts['SKIP']} SKIP** "
            f"(updated {data['platforms'][name]['updated']}; "
            + "; ".join(f"{family}: `{run['target']}`, protocol `{run['protocol']}`, "
                        f"profile `{run['profile']}`, lifecycle "
                        f"{'on' if run['mutating'] else 'off'}" for family, run in sorted(runs.items()))
            + ")")
    if args.capture_dir:
        lines.append(f"- Capture directory ({platform}, last run): `{args.capture_dir}`")
    lines.extend(("", "## Results by section", ""))

    def cell(name, family, key):
        return data["platforms"][name]["results"].get(family, {}).get(key)

    for section in sections:
        rows = []
        for case in section["cases"]:
            key = f"{case['protocol']}/{case['name']}"
            for family in case["families"]:
                cells = {name: cell(name, family, key) for name in platforms}
                if any(cells.values()):
                    rows.append((case, family, key, cells))
        if not rows:
            continue
        lines.extend((f"### {markdown_cell(section['title'])}", "",
                      markdown_cell(section["description"]), "",
                      "| Case | Protocol | Family | " + " | ".join(platforms) + " | Result / note |",
                      "|---|---|---|" + "---|" * len(platforms) + "---|"))
        for case, family, _key, cells in rows:
            notes = [f"{name}: {result[1]}" if len(platforms) > 1 else result[1]
                     for name, result in cells.items() if result and result[1] and result[0] != "PASS"]
            lines.append("| " + " | ".join((
                markdown_cell(case["name"]), markdown_cell(case["protocol"]), family,
                *(result[0] if result else "—" for result in cells.values()),
                markdown_cell("; ".join(notes) or case.get("description", "")),
            )) + " |")
        lines.append("")

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(data_path, "w", encoding="utf-8") as data_file:
        json.dump(data, data_file, indent=1, sort_keys=True)
        data_file.write("\n")
    with open(path, "w", encoding="utf-8") as report:
        report.write("\n".join(lines))


def write_results(path, args, cases, outcomes, corpus_path):
    """Machine-readable counterpart to the status report for branch comparisons."""
    with open(corpus_path, "rb") as corpus_file:
        corpus_sha256 = hashlib.sha256(corpus_file.read()).hexdigest()
    results = {
        "version": 1,
        "family": args.family,
        "profile": args.profile,
        "corpus_sha256": corpus_sha256,
        "cases": [
            {"protocol": case["protocol"], "name": case["name"],
             "status": outcomes[(case["protocol"], case["name"])][0],
             "detail": outcomes[(case["protocol"], case["name"])][1]}
            for case in cases
        ],
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as report:
        json.dump(results, report, indent=2)
        report.write("\n")


def exchange(sock, address, request):
    sock.sendto(request, address)
    response, _ = sock.recvfrom(2048)
    return response


def expect_silence(sock, address, request):
    sock.settimeout(0.25)
    sock.sendto(request, address)
    try:
        response, _ = sock.recvfrom(2048)
    except socket.timeout:
        return
    finally:
        sock.settimeout(TIMEOUT)
    raise AssertionError(f"expected packet to be ignored, got {len(response)} response bytes")


def pcp_request_for_client(version, opcode, lifetime, client_address):
    common = struct.pack("!BBHI", version, opcode, 0, lifetime)
    if ":" in client_address:
        client_ip = socket.inet_pton(socket.AF_INET6, client_address)
    else:
        client_ip = (b"\x00" * 10) + b"\xff\xff" + socket.inet_aton(client_address)
    return common + client_ip


def client_address(sock, address):
    probe = socket.socket(sock.family, socket.SOCK_DGRAM)
    try:
        probe.connect(address)
        return probe.getsockname()[0]
    finally:
        probe.close()


def pcp_map_request(version, lifetime, client_ip, internal_port, external_port,
                    protocol, nonce=b"\x42" * 12, external_ip=None, options=(), opcode=1):
    request = pcp_request_for_client(version, opcode, lifetime, client_ip)
    if version == 1:
        payload = (bytes((protocol, 0, 0, 0)) +
                   struct.pack("!HH", internal_port, external_port) +
                   (external_ip or (b"\x00" * 16)))
    else:
        payload = (nonce + bytes((protocol, 0, 0, 0)) +
                   struct.pack("!HH", internal_port, external_port) +
                   (external_ip or (b"\x00" * 16)))
    return request + payload + b"".join(options)


def encode_ip_address(value):
    if ":" in value:
        return socket.inet_pton(socket.AF_INET6, value)
    return b"\x00" * 10 + b"\xff\xff" + socket.inet_aton(value)


def resolve_value(value, variables):
    if isinstance(value, dict) and set(value) == {"add"}:
        return sum(resolve_value(part, variables) for part in value["add"])
    if isinstance(value, str) and value.startswith("$"):
        key = value[1:]
        if key not in variables:
            raise AssertionError(f"lifecycle variable {key!r} has not been set")
        return variables[key]
    return value


def resolve_request(value, variables):
    if isinstance(value, dict):
        if set(value) == {"add"}:
            return resolve_value(value, variables)
        return {key: resolve_request(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_request(item, variables) for item in value]
    return resolve_value(value, variables)


def build_lifecycle_request(sock, address, case, spec, step, variables):
    protocol = case["protocol"]
    lifetime = resolve_value(step["lifetime"], variables)
    if protocol == "pcp":
        client_ip = variables["client_ip"]
        external_port = resolve_value(step["external_port"], variables)
        internal_port = resolve_value(step.get("internal_port", "$internal_port"), variables)
        if spec["operation"] == "pcp_map":
            return pcp_map_request(spec["version"], lifetime, client_ip,
                                   internal_port, external_port,
                                   spec["protocol_number"], variables["nonce"],
                                   opcode=spec["opcode"])
        common = pcp_request_for_client(spec["version"], spec["opcode"], lifetime, client_ip)
        payload = (variables["nonce"] + bytes((spec["protocol_number"], 0, 0, 0)) +
                   struct.pack("!HH", internal_port, external_port) +
                   (b"\x00" * 16) +
                   struct.pack("!HH", spec["peer_port"], spec.get("peer_external_port", 0)) +
                   encode_ip_address(spec["peer_address"]))
        return common + payload

    public_port = resolve_value(step["public_port"], variables)
    return struct.pack("!BBHHHI", spec["version"], spec["opcode"], spec["reserved"],
                       variables["internal_port"], public_port, lifetime)


def capture_response(response, capture_spec):
    offset = capture_spec["offset"]
    size = {"u8": 1, "u16": 2, "u32": 4, "address": 16}.get(
        capture_spec["type"], capture_spec.get("length"))
    if size is None or offset < 0 or offset + size > len(response):
        raise AssertionError(f"response field at {offset} is truncated or has no length")
    if capture_spec["type"] == "u8":
        return response[offset]
    if capture_spec["type"] == "hex":
        return response[offset:offset + size].hex()
    if capture_spec["type"] == "address":
        return str(ipaddress.ip_address(response[offset:offset + size]))
    if capture_spec["type"] == "u16":
        return struct.unpack("!H", response[offset:offset + 2])[0]
    if capture_spec["type"] == "u32":
        return struct.unpack("!I", response[offset:offset + 4])[0]
    raise ValueError(f"unsupported capture type {capture_spec['type']}")


def assert_step_values(values, checks, variables):
    for field, rules in checks.items():
        actual = values[field]
        if "nonzero" in rules:
            assert (actual != 0) == rules["nonzero"], f"{field} unexpectedly has value {actual}"
        if "equals" in rules:
            wanted = resolve_value(rules["equals"], variables)
            assert actual == wanted, f"{field} was {actual}, expected {wanted}"
        if "not_equals" in rules:
            forbidden = resolve_value(rules["not_equals"], variables)
            assert actual != forbidden, f"{field} unexpectedly equals {forbidden}"
        if "min" in rules:
            assert actual >= resolve_value(rules["min"], variables), f"{field} {actual} is below {rules['min']}"
        if "max" in rules:
            assert actual <= resolve_value(rules["max"], variables), f"{field} {actual} exceeds {rules['max']}"
        if "remember" in rules:
            variables[rules["remember"]] = actual


def run_lifecycle_step(sock, address, case, spec, step, variables):
    step_variables = dict(variables)
    if "nonce_hex" in step:
        step_variables["nonce"] = bytes.fromhex(step["nonce_hex"])
    request = build_lifecycle_request(sock, address, case, spec, step, step_variables)
    response = exchange(sock, address, request)
    assert len(response) == spec["response_length"], f"expected {spec['response_length']}-byte lifecycle response, got {len(response)}"
    assert response[spec["version_offset"]] == spec["version"], "unexpected lifecycle response version"
    assert response[spec["opcode_offset"]] == spec["response_opcode"], "unexpected lifecycle response opcode"
    if case["protocol"] == "pcp":
        result = response[spec["result_offset"]]
    else:
        offset = spec["result_offset"]
        result = struct.unpack("!H", response[offset:offset + 2])[0]
    expected_results = step.get("result", spec["result"])
    assert result in expected_results, f"unexpected lifecycle result code {result}"
    if case["protocol"] == "pcp" and spec["version"] == 2:
        offset = spec["nonce_offset"]
        nonce = step_variables["nonce"]
        assert response[offset:offset + len(nonce)] == nonce, "lifecycle nonce mismatch"
    values = {field: capture_response(response, capture)
              for field, capture in spec["captures"].items()}
    assert_step_values(values, step.get("assert", {}), variables)
    return values


def run_lifecycle(sock, address, case):
    spec = dict(case["request"])
    spec.update(case["expect"])
    variables = {
        "internal_port": spec["internal_port_base"] + (os.getpid() % spec["port_range"]),
    }
    if case["protocol"] == "pcp":
        variables["client_ip"] = client_address(sock, address)
        variables["nonce"] = bytes.fromhex(spec["nonce_hex"]) if "nonce_hex" in spec else os.urandom(spec["nonce_length"])
    try:
        for step in spec["steps"]:
            run_lifecycle_step(sock, address, case, spec, step, variables)
    finally:
        cleanup = spec.get("cleanup")
        if cleanup:
            cleanup_steps = cleanup if isinstance(cleanup, list) else [cleanup]
            for cleanup_step in cleanup_steps:
                try:
                    run_lifecycle_step(sock, address, case, spec, cleanup_step, variables)
                except (AssertionError, OSError):
                    pass


def pcp_option(code, data=b""):
    option = struct.pack("!BBH", code, 0, len(data)) + data
    return option + (b"\x00" * (-len(data) % 4))


def client_ip_bytes(sock, address, selector):
    if selector == "unspecified":
        return b"\x00" * 16
    value = client_address(sock, address) if selector == "source" else selector
    if ":" in value:
        return socket.inet_pton(socket.AF_INET6, value)
    return b"\x00" * 10 + b"\xff\xff" + socket.inet_aton(value)


def pcp_options(options):
    encoded = []
    for option in options:
        if "raw_hex" in option:
            encoded.append(bytes.fromhex(option["raw_hex"]))
        else:
            encoded.append(pcp_option(option["code"], bytes.fromhex(option.get("data_hex", ""))))
    return b"".join(encoded)


def build_request(sock, address, spec):
    request = build_request_payload(sock, address, spec)
    for patch in spec.get("patches", []):
        offset = patch["offset"]
        data = bytes.fromhex(patch["hex"])
        if offset < 0 or offset + len(data) > len(request):
            raise ValueError("packet patch falls outside the request")
        request = request[:offset] + data + request[offset + len(data):]
    return request


def build_request_payload(sock, address, spec):
    kind = spec["kind"]
    if kind == "raw":
        return bytes.fromhex(spec["hex"])
    if kind == "pcp_header":
        request = struct.pack("!BBHI", spec["version"], spec["opcode"],
                              spec.get("reserved", 0), spec.get("lifetime", 0))
        request += client_ip_bytes(sock, address, spec.get("client", "unspecified"))
        if spec.get("response_bit"):
            request = request[:1] + bytes((request[1] | 0x80,)) + request[2:]
        request += bytes.fromhex(spec.get("append_hex", ""))
        if "size" in spec:
            request = request.ljust(spec["size"], b"\x00")
        if "truncate_to" in spec:
            request = request[:spec["truncate_to"]]
        return request
    if kind in {"pcp_map", "pcp_peer"}:
        version = spec["version"]
        nonce = bytes.fromhex(spec.get("nonce_hex", "42" * 12))
        common = struct.pack("!BBHI", version, spec.get("opcode", 1),
                             spec.get("reserved", 0), spec.get("lifetime", 0))
        common += client_ip_bytes(sock, address, spec.get("client", "unspecified"))
        protocol_number = spec["protocol_number"]
        mapping = (nonce if version == 2 else b"")
        mapping += bytes((protocol_number, 0, 0, 0))
        mapping += struct.pack("!HH", spec.get("internal_port", 40000), spec.get("external_port", 0))
        external_address = spec.get("external_address", "::")
        if ":" in external_address:
            mapping += socket.inet_pton(socket.AF_INET6, external_address)
        else:
            mapping += client_ip_bytes(sock, address, external_address)
        if kind == "pcp_peer":
            if version != 2:
                raise ValueError("pcp_peer request builder requires version 2")
            mapping += struct.pack("!HH", spec["peer_port"], spec.get("peer_reserved", 0))
            mapping += encode_ip_address(spec["peer_address"])
        request = common + mapping + pcp_options(spec.get("options", []))
        request += bytes.fromhex(spec.get("append_hex", ""))
        if "truncate_to" in spec:
            request = request[:spec["truncate_to"]]
        return request
    if kind == "pcp_auth":
        header = bytes((2, spec["opcode"], 0, spec["result_code"]))
        header += struct.pack("!I", 0) + (b"\x00" * 16)
        body = struct.pack("!II", spec["session_id"], spec["sequence_number"])
        return header + body + pcp_options(spec.get("options", []))
    if kind == "natpmp":
        return struct.pack("!BBHHHI", spec["version"], spec["opcode"],
                           spec.get("reserved", 0), spec.get("private_port", 0),
                           spec.get("public_port", 0), spec.get("lifetime", 0))
    raise ValueError(f"unsupported request kind: {kind}")


def parse_auth_response(response):
    assert len(response) >= 32, f"short PCP AUTHENTICATION response: {len(response)} bytes"
    assert response[0] == 2 and response[1] == 131, "expected PCP AUTHENTICATION response"
    session_id, sequence_number = struct.unpack("!II", response[24:32])
    options = {}
    offset = 32
    while offset < len(response):
        assert len(response) - offset >= 4, "truncated PCP authentication option header"
        code, _reserved, length = struct.unpack("!BBH", response[offset:offset + 4])
        start, end = offset + 4, offset + 4 + length
        assert end <= len(response), f"truncated PCP authentication option {code}"
        options[code] = response[start:end]
        offset = end + (-length % 4)
    return response[3], session_id, sequence_number, options


def expect_no_auth_progress(sock, address, request, previous_response):
    """Reject a sequence error; an identical response is only a valid retransmission."""
    sock.settimeout(0.25)
    sock.sendto(request, address)
    try:
        response, _ = sock.recvfrom(2048)
        assert response == previous_response, "invalid PA sequence advanced the authentication exchange"
    except socket.timeout:
        pass
    finally:
        sock.settimeout(TIMEOUT)


def run_auth_sequence(sock, address, case):
    """Run a YAML-described PA session and sequence-number probes."""
    spec = case["request"]
    expectation = case["expect"]
    initiation = build_request(sock, address, spec["initiation"])
    response = exchange(sock, address, initiation)
    result, session_id, server_sequence, options = parse_auth_response(response)
    assert result == expectation["initiation_result"], f"unexpected PA-Initiation result {result}"
    assert session_id != 0, "server did not assign a PA session ID"
    assert server_sequence == expectation["initial_server_sequence"], \
        f"initial server sequence was {server_sequence}"
    eap_request = options.get(7)
    assert eap_request and len(eap_request) >= 5, "PA-Server response lacks an EAP request"
    assert eap_request[0] == 1 and eap_request[4] == 1, \
        "test profile requires an EAP Request/Identity from the server"

    def identity_reply(sequence_number, identity):
        identity_bytes = identity.encode("utf-8")
        eap_length = 5 + len(identity_bytes)
        eap = struct.pack("!BBH", 2, eap_request[1], eap_length) + bytes((1,)) + identity_bytes
        message = bytes((2, 3, 0, 23)) + (b"\x00" * 4) + (b"\x00" * 16)
        message += struct.pack("!II", session_id, sequence_number)
        return message + pcp_option(7, eap)

    followup = expectation["followup"]
    followup_request = identity_reply(followup["sequence_number"], followup["identity"])
    if followup["behavior"] == "discard":
        expect_no_auth_progress(sock, address, followup_request, response)
        previous_response = response
    else:
        followup_response = exchange(sock, address, followup_request)
        result, response_session, response_sequence, _ = parse_auth_response(followup_response)
        assert response_session == session_id, "PA response changed the session ID"
        assert result in followup["result_codes"], f"unexpected PA response result {result}"
        assert response_sequence == followup["response_sequence"], \
            f"server sequence was {response_sequence}, expected {followup['response_sequence']}"
        previous_response = followup_response

    replay = expectation.get("replay")
    if replay:
        replay_request = identity_reply(replay["sequence_number"], replay["identity"])
        expect_no_auth_progress(sock, address, replay_request, previous_response)


def check_protocol_response(request, response, protocol, spec, variables):
    expect = spec["expect"]
    assert len(response) >= 4, "response header is truncated"
    if protocol == "pcp":
        assert response[0] == expect["version"], f"unexpected PCP version {response[0]}"
        assert response[1] == expect["opcode"], f"unexpected PCP opcode {response[1]}"
        result = response[3]
    elif protocol == "natpmp":
        assert response[0] == expect["version"], f"unexpected NAT-PMP version {response[0]}"
        assert response[1] == expect["opcode"], f"unexpected NAT-PMP opcode {response[1]}"
        result = struct.unpack("!H", response[2:4])[0]
    else:
        raise ValueError(f"unsupported protocol in sequence: {protocol}")
    assert result in expect["result"], f"unexpected {protocol} result code {result}"
    expected_length = len(request) if expect["length"] == "request" else expect["length"]
    assert len(response) in (expected_length if isinstance(expected_length, list) else [expected_length]), \
        f"expected {expected_length}-byte {protocol} response, got {len(response)}"
    assert_response_echo(request, response, expect)
    values = {name: capture_response(response, field)
              for name, field in spec.get("captures", {}).items()}
    assert_step_values(values, spec.get("assert", {}), variables)


def run_traffic_probe(sock, address, spec, variables, peer_url):
    """A WAN actor probes a temporary LAN listener; both ends must agree."""
    spec = resolve_request(spec, variables)
    if spec["expect"] not in {"delivered", "blocked"}:
        raise ValueError("traffic_probe expect must be delivered or blocked")
    if not 1 <= spec["external_port"] <= 65535:
        if spec["expect"] == "blocked" and spec.get("skip_unrepresentable_port"):
            print(f"INFO: adjacent port {spec['external_port']} is outside the transport port space")
            return
        raise AssertionError("traffic_probe external port is outside 1..65535")
    kind = {"tcp": socket.SOCK_STREAM, "udp": socket.SOCK_DGRAM}[spec["transport"]]
    token = os.urandom(24)
    received = []
    with socket.socket(sock.family, kind) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((client_address(sock, address), spec["internal_port"]))
        listener.settimeout(1.0)
        if kind == socket.SOCK_STREAM:
            listener.listen(1)

        def echo():
            try:
                if kind == socket.SOCK_DGRAM:
                    data, remote = listener.recvfrom(4096)
                    received.append(data)
                    listener.sendto(data, remote)
                else:
                    connection, _remote = listener.accept()
                    with connection:
                        connection.settimeout(1.0)
                        data = b""
                        while len(data) < len(token):
                            block = connection.recv(4096)
                            if not block:
                                break
                            data += block
                        received.append(data)
                        connection.sendall(data)
            except OSError:
                pass

        worker = threading.Thread(target=echo, daemon=True)
        worker.start()
        query = json.dumps({"protocol": spec["transport"], "host": spec["external_address"],
                            "port": spec["external_port"], "token_hex": token.hex()}).encode()
        try:
            request = urllib.request.Request(peer_url + "/probe", data=query,
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=3) as result:
                outcome = json.load(result)
        finally:
            worker.join(timeout=1.5)
    expected = spec["expect"] == "delivered"
    assert outcome["delivered"] == expected and (token in received) == expected, \
        f"{spec['transport']} port {spec['external_port']}: expected {spec['expect']}, got {outcome}, LAN received={bool(received)}"


def run_protocol_step(sock, address, step, variables, peer_url=None):
    if "delay_seconds" in step:
        time.sleep(resolve_value(step["delay_seconds"], variables))
    if step.get("kind") == "traffic_probe":
        run_traffic_probe(sock, address, step, variables, peer_url)
        return
    protocol = step["protocol"]
    request = build_request(sock, address, resolve_request(step["request"], variables))
    expected = list(step.get("responses", [step]))
    sock.sendto(request, address)
    for _ in range(len(expected)):
        response, _sender = sock.recvfrom(2048)
        errors = []
        for index, spec in enumerate(expected):
            candidate_variables = dict(variables)
            try:
                check_protocol_response(request, response, protocol, spec, candidate_variables)
            except AssertionError as error:
                errors.append(str(error))
                continue
            variables.update(candidate_variables)
            del expected[index]
            break
        else:
            raise AssertionError("response matched none of the expected replies: " + "; ".join(errors))
    if step.get("no_extra_responses"):
        previous_timeout = sock.gettimeout()
        try:
            sock.settimeout(0.25)
            try:
                extra, _sender = sock.recvfrom(2048)
            except socket.timeout:
                pass
            else:
                raise AssertionError(f"unexpected extra response ({len(extra)} bytes)")
        finally:
            sock.settimeout(previous_timeout)


def run_protocol_sequence(sock, address, case, peer_url=None):
    """Execute an ordered PCP/NAT-PMP exchange described entirely in YAML."""
    sequence = case["request"]
    variables = {}
    try:
        for index, step in enumerate(sequence["steps"], 1):
            try:
                run_protocol_step(sock, address, step, variables, peer_url)
            except (AssertionError, OSError) as error:
                operation = step.get("kind", step.get("request", {}).get("kind", "exchange"))
                raise AssertionError(f"step {index} ({operation}): {error}") from error
    finally:
        for cleanup in sequence.get("cleanup", []):
            try:
                run_protocol_step(sock, address, cleanup, variables, peer_url)
            except (AssertionError, OSError):
                pass


PCAP_MAGICS = {
    b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">",   # microsecond
    b"\x4d\x3c\xb2\xa1": "<", b"\xa1\xb2\x3c\x4d": ">",   # nanosecond
}


def read_pcap_frames(path):
    """Return the Ethernet frames of a classic pcap file written by tcpdump -w."""
    with open(path, "rb") as pcap:
        data = pcap.read()
    if len(data) < 24 or data[:4] not in PCAP_MAGICS:
        raise AssertionError(f"{path}: not a classic pcap file")
    endian = PCAP_MAGICS[data[:4]]
    linktype = struct.unpack(endian + "I", data[20:24])[0]
    if linktype != 1:
        raise AssertionError(f"{path}: unsupported link type {linktype} (need Ethernet)")
    frames, offset = [], 24
    while offset + 16 <= len(data):
        captured = struct.unpack(endian + "I", data[offset + 8:offset + 12])[0]
        frames.append(data[offset + 16:offset + 16 + captured])
        offset += 16 + captured
    return frames


def parse_udp_frame(frame):
    """Return (source ip, destination ip, source port, destination port, payload) or None."""
    if len(frame) < 14:
        return None
    ethertype, offset = struct.unpack("!H", frame[12:14])[0], 14
    while ethertype in (0x8100, 0x88a8) and len(frame) >= offset + 4:  # VLAN tags
        ethertype = struct.unpack("!H", frame[offset + 2:offset + 4])[0]
        offset += 4
    if ethertype == 0x0800 and len(frame) >= offset + 20:
        header_length = (frame[offset] & 0x0f) * 4
        if frame[offset + 9] != 17 or len(frame) < offset + header_length + 8:
            return None
        source = ipaddress.ip_address(frame[offset + 12:offset + 16])
        destination = ipaddress.ip_address(frame[offset + 16:offset + 20])
        udp = offset + header_length
    elif ethertype == 0x86dd and len(frame) >= offset + 48:
        if frame[offset + 6] != 17:  # extension headers are not expected here
            return None
        source = ipaddress.ip_address(frame[offset + 8:offset + 24])
        destination = ipaddress.ip_address(frame[offset + 24:offset + 40])
        udp = offset + 40
    else:
        return None
    source_port, destination_port = struct.unpack("!HH", frame[udp:udp + 4])
    return source, destination, source_port, destination_port, frame[udp + 8:]


def matching_udp_packets(path, expect, family, check_payload):
    """UDP packets in a capture that go to the expected multicast destination."""
    destination = ipaddress.ip_address(expect["destinations"][family])
    matches = []
    for frame in read_pcap_frames(path):
        packet = parse_udp_frame(frame)
        if packet is None or packet[1] != destination or packet[3] != expect["destination_port"]:
            continue
        if check_payload:
            payload = packet[4]
            if (packet[2] != expect["source_port"] or len(payload) != expect["length"]
                    or payload[0] != expect["version"] or payload[1] != expect["opcode"]
                    or payload[3] not in expect["result"]):
                continue
        matches.append(packet)
    return matches


def run_daemon_restart(case, args):
    """Cold-start the daemon through the fixture and check the unsolicited traffic it records."""
    expect = case["expect"]
    if expect["kind"] != "unsolicited_announce":
        raise AssertionError("daemon_restart needs an unsolicited_announce expectation")
    if not args.fixture:
        raise AssertionError("daemon_restart requires --fixture")
    if args.capture_dir:
        directory = os.path.join(args.capture_dir, f"{args.family}-{case['protocol']}-{case['name']}")
        os.makedirs(directory, exist_ok=True)
        cleanup = None
    else:
        directory = cleanup = tempfile.mkdtemp(prefix="pcp-fixture-")
    try:
        # The fixture contract: restart the daemon from a cold start while recording its outgoing
        # packets, then leave lan-N.pcap and wan-N.pcap (one per interface) in DIRECTORY.
        result = subprocess.run(shlex.split(args.fixture) + ["daemon-restart", directory, args.family],
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise AssertionError("fixture daemon-restart failed: " + (result.stderr or result.stdout).strip())
        lan = sorted(name for name in os.listdir(directory) if re.fullmatch(r"lan-\d+\.pcap", name))
        wan = sorted(name for name in os.listdir(directory) if re.fullmatch(r"wan-\d+\.pcap", name))
        if not lan or not wan:
            raise AssertionError("fixture produced no LAN or WAN capture")
        problems = []
        if expect["lan"] == "every":
            for name in lan:
                if not matching_udp_packets(os.path.join(directory, name), expect, args.family, True):
                    problems.append(f"no ANNOUNCE to {expect['destinations'][args.family]} on {name[:-5]}")
        if expect["wan"] == "none":
            for name in wan:
                leaked = matching_udp_packets(os.path.join(directory, name), expect, args.family, False)
                if leaked:
                    problems.append(f"{len(leaked)} ANNOUNCE packet(s) to {expect['destinations'][args.family]} "
                                    f"leaked on {name[:-5]}")
        assert not problems, "; ".join(problems)
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)


def run_case(sock, address, case, mutating=False, peer_url=None, args=None):
    request_spec = case["request"]
    expect = case["expect"]
    if request_spec["kind"] == "daemon_restart":
        run_daemon_restart(case, args)
        return
    if request_spec["kind"] == "pcp_auth_sequence":
        run_auth_sequence(sock, address, case)
        return
    if request_spec["kind"] == "protocol_sequence":
        run_protocol_sequence(sock, address, case, peer_url)
        return
    if expect["kind"] == "lifecycle":
        if not mutating:
            raise AssertionError("mapping lifecycle requires --mutating")
        run_lifecycle(sock, address, case)
        return

    request = build_request(sock, address, request_spec)
    if expect["kind"] == "silence":
        expect_silence(sock, address, request)
        return

    response = exchange(sock, address, request)
    expected_length = (None if "min_length" in expect else
                       len(request) if expect["length"] == "request" else expect["length"])
    if case["protocol"] == "pcp":
        version_offset = expect.get("version_offset", 0)
        opcode_offset = expect.get("opcode_offset", 1)
        result_offset = expect.get("result_offset", 3)
        reserved_offset = expect.get("reserved_offset", 2)
        assert response[version_offset] == expect["version"], f"unexpected PCP response version {response[version_offset]}"
        assert response[opcode_offset] == expect["opcode"], f"unexpected PCP response opcode {response[opcode_offset]}"
        assert response[reserved_offset] == expect.get("reserved", 0), "bad PCP response reserved byte"
        result = response[result_offset]
    else:
        version_offset = expect.get("version_offset", 0)
        opcode_offset = expect.get("opcode_offset", 1)
        result_offset = expect.get("result_offset", 2)
        assert response[version_offset] == expect["version"], f"unexpected NAT-PMP response version {response[version_offset]}"
        assert response[opcode_offset] == expect["opcode"], f"unexpected NAT-PMP response opcode {response[opcode_offset]}"
        result = struct.unpack("!H", response[result_offset:result_offset + 2])[0]
    assert result in expect["result"], f"unexpected result code {result}"
    if expected_length is None:
        assert len(response) >= expect["min_length"], f"expected at least {expect['min_length']} response bytes, got {len(response)}"
    else:
        assert len(response) == expected_length, f"expected {expected_length}-byte response, got {len(response)}"
    assert_response_echo(request, response, expect)
    for field in expect.get("fields", []):
        value = capture_response(response, field)
        if "nonzero" in field:
            assert (value != 0) == field["nonzero"], f"{field['description']} has unexpected value {value}"
        if "equals" in field:
            assert value == field["equals"], f"{field['description']} was {value}, expected {field['equals']}"
    if expect.get("required_options"):
        option_offset = expect.get("options_offset", 24)
        found = []
        while option_offset < len(response):
            assert len(response) - option_offset >= 4, "truncated option header in response"
            code, _reserved, option_length = struct.unpack("!BBH", response[option_offset:option_offset + 4])
            data_start = option_offset + 4
            data_end = data_start + option_length
            assert data_end <= len(response), f"truncated response option {code}"
            found.append((code, response[data_start:data_end]))
            option_offset = data_start + ((option_length + 3) & ~3)
        for required in expect["required_options"]:
            matching = [data for code, data in found if code == required["code"]]
            assert matching, f"required response option {required['code']} missing"
            if "data_hex" in required:
                assert bytes.fromhex(required["data_hex"]) in matching, f"response option {required['code']} data mismatch"
            if "minimum_length" in required:
                assert any(len(data) >= required["minimum_length"] for data in matching), f"response option {required['code']} too short"


def run_self_tests():
    """Check runner mechanics without loading cases or contacting a daemon."""
    import unittest
    from pathlib import Path
    from unittest.mock import patch

    runner = sys.modules[__name__]

    class SocketFixture:
        family = socket.AF_INET

        def __init__(self, responses=()):
            self.responses = list(responses)
            self.sent = []
            self.timeout = TIMEOUT

        def sendto(self, data, address):
            self.sent.append((data, address))

        def recvfrom(self, size):
            if not self.responses:
                raise socket.timeout()
            return self.responses.pop(0), ("192.0.2.1", 5351)

        def settimeout(self, timeout):
            self.timeout = timeout

        def gettimeout(self):
            return self.timeout

    class RunnerTests(unittest.TestCase):
        def test_variables_and_captures(self):
            self.assertEqual(resolve_request(
                {"port": {"add": ["$start", 3]}, "values": ["$start"]}, {"start": 1000}),
                {"port": 1003, "values": [1000]})
            self.assertEqual(resolve_value({"add": ["$ttl", 0.5]}, {"ttl": 2}), 2.5)
            with self.assertRaisesRegex(AssertionError, "has not been set"):
                resolve_value("$missing", {})
            reply = bytes.fromhex("010203040506")
            for field, expected in (({"type": "u8", "offset": 0}, 1),
                                    ({"type": "u16", "offset": 1}, 0x0203),
                                    ({"type": "u32", "offset": 2}, 0x03040506),
                                    ({"type": "hex", "offset": 0, "length": 3}, "010203")):
                with self.subTest(field=field):
                    self.assertEqual(capture_response(reply, field), expected)
            with self.assertRaisesRegex(AssertionError, "truncated"):
                capture_response(reply, {"type": "u16", "offset": len(reply) - 1})

        def test_response_order_and_duplicates(self):
            # Synthetic NAT-PMP replies exercise matching independently of case names.
            replies = [struct.pack("!BBHII", 0, 128, 0, 1, value) for value in (1, 2)]
            step = {"protocol": "natpmp", "request": {"kind": "raw", "hex": "0000"},
                    "no_extra_responses": True,
                    "responses": [{"expect": {"version": 0, "opcode": 128, "result": [0],
                                              "length": len(replies[0])},
                                   "captures": {"value": {"type": "u32", "offset": 8}},
                                   "assert": {"value": {"equals": value}}} for value in (1, 2)]}
            for responses in (replies, replies[::-1]):
                sock = SocketFixture(responses)
                run_protocol_step(sock, ("192.0.2.1", 5351), step, {})
                self.assertEqual(len(sock.sent), 1)
                self.assertEqual(sock.timeout, TIMEOUT)
            for responses in ([replies[0], replies[0]], replies + replies[:1]):
                sock = SocketFixture(responses)
                with self.subTest(responses=responses), self.assertRaises(AssertionError):
                    run_protocol_step(sock, ("192.0.2.1", 5351), step, {})
                self.assertEqual(sock.timeout, TIMEOUT)

        def test_response_fields(self):
            reply = struct.pack("!BBHII", 0, 128, 0, 1, 0)
            spec = {"expect": {"version": 0, "opcode": 128, "result": [0], "length": len(reply)},
                    "captures": {"reserved": {"type": "u32", "offset": 8}},
                    "assert": {"reserved": {"equals": 0}}}
            check_protocol_response(b"", reply, "natpmp", spec, {})
            for offset in (0, 1, 3, 8, 9, 10, 11):
                bad = bytearray(reply)
                bad[offset] ^= 1
                with self.subTest(offset=offset), self.assertRaises(AssertionError):
                    check_protocol_response(b"", bytes(bad), "natpmp", spec, {})
            with self.assertRaises(AssertionError):
                check_protocol_response(b"", reply[:-1], "natpmp", spec, {})

        def test_traffic_port_bounds(self):
            with self.assertRaisesRegex(AssertionError, "outside"):
                run_traffic_probe(SocketFixture(), None,
                                  {"expect": "delivered", "external_port": 65536}, {}, None)
            run_traffic_probe(SocketFixture(), None,
                              {"expect": "blocked", "external_port": 65536,
                               "skip_unrepresentable_port": True}, {}, None)

        def test_unsolicited_announce_fixture(self):
            payload = bytes([2, 128, 0, 0]) + bytes(20)
            udp = struct.pack("!HHHH", 5351, 5350, 8 + len(payload), 0) + payload
            ethernet = bytes(12)
            ipv4 = (ethernet + b"\x08\x00" + bytes([0x45, 0, 0, 0, 0, 0, 0, 0, 1, 17, 0, 0])
                    + bytes([192, 168, 1, 1]) + bytes([224, 0, 0, 1]) + udp)
            ipv6 = (ethernet + b"\x86\xdd" + bytes([0x60, 0, 0, 0, 0, len(udp), 17, 1])
                    + ipaddress.ip_address("fd42::1").packed + ipaddress.ip_address("ff02::1").packed + udp)

            def pcap(*frames):
                data = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)
                for frame in frames:
                    data += struct.pack("<IIII", 0, 0, len(frame), len(frame)) + frame
                return data

            case = {"name": "x", "protocol": "pcp",
                    "expect": {"kind": "unsolicited_announce", "source_port": 5351,
                               "destination_port": 5350, "length": 24, "version": 2, "opcode": 128,
                               "result": [0], "lan": "every", "wan": "none",
                               "destinations": {"ipv4": "224.0.0.1", "ipv6": "ff02::1"}}}

            def run(files, family):
                def fake(command, **_kwargs):
                    for name, data in files.items():
                        with open(os.path.join(command[-2], name), "wb") as handle:
                            handle.write(data)
                    return subprocess.CompletedProcess(command, 0, "", "")
                args = type("Args", (), {"fixture": "fixture.sh", "capture_dir": None, "family": family})
                with patch.object(runner.subprocess, "run", fake):
                    run_daemon_restart(case, args)

            both = {"lan-0.pcap": pcap(ipv4, ipv6), "lan-1.pcap": pcap(ipv4, ipv6), "wan-0.pcap": pcap()}
            for family in ("ipv4", "ipv6"):
                run(both, family)
            with self.assertRaisesRegex(AssertionError, "no ANNOUNCE .* on lan-1"):
                run({**both, "lan-1.pcap": pcap(ipv6)}, "ipv4")
            with self.assertRaisesRegex(AssertionError, "leaked on wan-0"):
                run({**both, "wan-0.pcap": pcap(ipv4)}, "ipv4")
            run({**both, "wan-0.pcap": pcap(ipv6)}, "ipv4")  # other family is not this case's leak
            with self.assertRaisesRegex(AssertionError, "no LAN or WAN capture"):
                run({"wan-0.pcap": pcap()}, "ipv4")

        def test_status_report_merges_platforms(self):
            import tempfile
            case = {"name": "startup", "protocol": "pcp", "families": ["ipv4"], "description": "d"}
            other = {"name": "other", "protocol": "pcp", "families": ["ipv4"], "description": "o"}
            sections = [{"title": "T", "description": "D", "cases": [case, other]}]

            def report(platform, outcomes):
                args = type("Args", (), {"platform": platform, "family": "ipv4", "host": "h", "port": 1,
                                         "protocol": "pcp", "profile": "baseline", "mutating": True,
                                         "capture_dir": None})
                write_status_report(path, args, sections, outcomes)
                with open(path, encoding="utf-8") as handle:
                    return handle.read()

            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "status.md")
                report("linux", {("pcp", "startup"): ("PASS", ""), ("pcp", "other"): ("PASS", "")})
                text = report("freebsd", {("pcp", "startup"): ("FAIL", "leaked on wan-0"),
                                          ("pcp", "other"): ("SKIP", "not selected for this invocation")})
                self.assertIn("| startup | pcp | ipv4 | PASS | FAIL | freebsd: leaked on wan-0 |", text)
                self.assertIn("| other | pcp | ipv4 | PASS | — |", text)
                # A later Linux run replaces only Linux's column.
                text = report("linux", {("pcp", "startup"): ("FAIL", "x"), ("pcp", "other"): ("PASS", "")})
                self.assertIn("| startup | pcp | ipv4 | FAIL | FAIL | linux: x; freebsd: leaked on wan-0 |", text)

        def test_traffic_agent_integration(self):
            # Local sockets only; no daemon, Docker, or external network is used.
            common = str(Path(__file__).resolve().parents[1] / "common")
            with patch.object(sys, "path", [common] + sys.path):
                from traffic_agent import create_server
            server = create_server(port=0, source4="127.0.0.1", source6="::1")
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for transport, kind in (("udp", socket.SOCK_DGRAM), ("tcp", socket.SOCK_STREAM)):
                    with self.subTest(transport=transport):
                        # Pick an available port for the probe's echo listener.
                        with socket.socket(socket.AF_INET, kind) as reserve:
                            reserve.bind(("127.0.0.1", 0))
                            port = reserve.getsockname()[1]
                        spec = {"transport": transport, "internal_port": port,
                                "external_port": port, "external_address": "::ffff:127.0.0.1",
                                "expect": "delivered"}
                        with patch.object(runner, "client_address", return_value="127.0.0.1"):
                            run_traffic_probe(SocketFixture(), ("127.0.0.1", 5351), spec, {},
                                              f"http://127.0.0.1:{server.server_port}")
            finally:
                server.shutdown()
                server.server_close()
                worker.join()

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RunnerTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true",
                        help="check runner mechanics using simulated replies and loopback sockets; no daemon needed")
    parser.add_argument("--host", default="127.0.0.1", help="server IPv4 address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5351, help="server UDP port (default: 5351)")
    parser.add_argument("--protocol", choices=("pcp", "natpmp", "both"), default="both")
    parser.add_argument("--family", choices=("ipv4", "ipv6"), default="ipv4")
    parser.add_argument("--profile", default="baseline",
                        help="explicit server fixture profile from YAML (default: baseline)")
    parser.add_argument("--peer-url", help="WAN traffic actor URL for cases requiring the wan-peer fixture")
    parser.add_argument("--fixture", metavar="COMMAND",
                        help="platform fixture command for cases requiring daemon-restart; it is run as "
                             "COMMAND daemon-restart DIRECTORY FAMILY and must restart the daemon while "
                             "recording lan-N.pcap and wan-N.pcap into DIRECTORY")
    parser.add_argument("--unsupported", action="append", default=[], metavar="FEATURE",
                        help="feature this platform's daemon lacks (for example pcp-peer, which needs the "
                             "netfilter backend); cases requiring it are reported as SKIP. May be repeated")
    parser.add_argument("--fixture-only", action="store_true",
                        help="run only the cases that need a fixture this invocation provides")
    parser.add_argument("--case", action="append", help="run one named case; may be repeated")
    parser.add_argument("--allow-empty-selection", action="store_true",
                        help="write SKIP results if selection excludes this profile/family/protocol")
    parser.add_argument("--mutating", action="store_true", default=True,
                        help="include mapping create/renew/delete cases (default; use a test gateway)")
    parser.add_argument("--no-mutating", dest="mutating", action="store_false",
                        help="skip mapping create/renew/delete cases")
    parser.add_argument("--status-file",
                        default=os.path.join(os.path.dirname(__file__), "status.md"),
                        help="combined Markdown report path (default: status.md beside this script)")
    parser.add_argument("--status-data-file", default=None,
                        help="per-platform results behind the report (default: status-data.json "
                             "beside the status file)")
    parser.add_argument("--platform", default="linux",
                        help="platform column this run fills in the status report (default: linux)")
    parser.add_argument("--results-file",
                        help="JSON results path (default with captures: results-FAMILY.json in the run folder)")
    parser.add_argument("--capture-dir",
                        help="capture root; save each run in a timestamped subdirectory")
    parser.add_argument("--capture-name", type=capture_name,
                        help="run folder label (default: the single --case name, otherwise all)")
    parser.add_argument("--capture-interface", default="any",
                        help="interface passed to tcpdump (default: any)")
    parser.add_argument("--tcpdump", default="tcpdump",
                        help="tcpdump executable (default: tcpdump)")
    parser.add_argument("--capture-timeout", type=float, default=1.0,
                        help="seconds to wait for tcpdump to stop after each case")
    args = parser.parse_args()
    if args.self_test:
        return run_self_tests()
    try:
        import yaml
    except ImportError as error:
        raise SystemExit("PyYAML is required (install python3-yaml)") from error
    if args.capture_name and not args.capture_dir:
        parser.error("--capture-name requires --capture-dir")

    corpus_path = os.path.join(os.path.dirname(__file__), "pcp-natpmp-cases.yaml")
    with open(corpus_path, encoding="utf-8") as corpus_file:
        corpus = yaml.safe_load(corpus_file)
    if corpus.get("version") != 1:
        parser.error("unsupported YAML corpus version")
    profiles = corpus.get("profiles", {"baseline": {}})
    if args.profile not in profiles:
        parser.error("unknown server profile; choose " + ", ".join(profiles))
    sections = corpus.get("sections")
    if not isinstance(sections, list):
        parser.error("YAML corpus must define readable sections")
    cases = []
    for section in sections:
        if not {"title", "description", "cases"}.issubset(section):
            parser.error("each YAML section needs title, description and cases")
        for case in section["cases"]:
            case["section"] = section["title"]
            cases.append(case)
    request_kinds = {"raw", "pcp_header", "pcp_map", "pcp_peer", "pcp_auth", "pcp_auth_sequence", "natpmp", "lifecycle", "protocol_sequence", "daemon_restart"}
    for case in cases:
        if not set(case.get("profiles", profiles)).issubset(profiles):
            parser.error(f"{case['name']}: unknown server profile")
        if not {"name", "protocol", "families", "request", "expect"}.issubset(case):
            parser.error("every YAML case needs name, protocol, families, request and expect")
        if case["request"].get("kind") not in request_kinds:
            parser.error(f"unsupported request kind in case {case['name']}")
        if case["expect"].get("kind") not in {"response", "silence", "lifecycle", "auth_sequence", "unsolicited_announce"}:
            parser.error(f"unsupported expectation kind in case {case['name']}")
        if case["expect"]["kind"] == "response":
            required = {"version", "opcode", "result"}
            if not required.issubset(case["expect"]) or not ({"length", "min_length"} & set(case["expect"])):
                parser.error(f"response expectation incomplete in case {case['name']}")
    names = [case["name"] for case in cases]
    identities = [(case["protocol"], case["name"]) for case in cases]
    if len(identities) != len(set(identities)):
        parser.error("duplicate case names in YAML corpus")
    earlier_cases = {}
    for case in cases:
        dependencies = case.get("depends_on", [])
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
            parser.error("depends_on must be a list of protocol/case names")
        for dependency in dependencies:
            if dependency not in earlier_cases:
                parser.error(f"{case['name']}: prerequisite {dependency} must be an earlier case")
            prerequisite = earlier_cases[dependency]
            if not set(case["families"]).issubset(prerequisite["families"]):
                parser.error(f"{case['name']}: prerequisite {dependency} lacks a required family")
            if not set(case.get("profiles", profiles)).issubset(prerequisite.get("profiles", profiles)):
                parser.error(f"{case['name']}: prerequisite {dependency} lacks a required profile")
        if set(case.get("requires", [])) - {"wan-peer", "daemon-restart", "pcp-peer"}:
            parser.error(f"{case['name']}: unknown required fixture")
        earlier_cases[f"{case['protocol']}/{case['name']}"] = case
    if args.family == "ipv6":
        if args.protocol == "natpmp":
            parser.error("NAT-PMP uses IPv4; select --protocol pcp for IPv6")
        args.protocol = "pcp"
    selected = []
    for case in cases:
        if args.profile not in case.get("profiles", profiles):
            continue
        if args.protocol != "both" and case["protocol"] not in (args.protocol, "both"):
            continue
        if args.family not in case["families"]:
            continue
        if args.fixture_only and not (set(case.get("requires", [])) & {"daemon-restart"}):
            continue
        if args.case is None or case["name"] in args.case:
                selected.append(case)
    if args.case:
        unknown = set(args.case) - set(names)
        if unknown:
            parser.error("unknown case(s): " + ", ".join(sorted(unknown)))
        selected = [case for case in selected if case["name"] in args.case]
        if not selected and not args.allow_empty_selection:
            parser.error("the requested case does not match --profile/--protocol/--family")
        mutating_cases = {case["name"] for case in cases if case.get("mutating")}
        if not args.mutating and any(case["name"] in mutating_cases for case in selected):
            parser.error("mapping lifecycle cases require --mutating")
    elif not args.mutating:
        mutating_cases = {case["name"] for case in cases if case.get("mutating")}
        selected = [case for case in selected if case["name"] not in mutating_cases]

    selected_keys = {f"{case['protocol']}/{case['name']}" for case in selected}
    def include_dependencies(key):
        for dependency in earlier_cases[key].get("depends_on", []):
            selected_keys.add(dependency)
            include_dependencies(dependency)
    for key in list(selected_keys):
        include_dependencies(key)
    selected = [case for case in cases
                if f"{case['protocol']}/{case['name']}" in selected_keys]

    prepare_capture_directory(args)
    address = (args.host, args.port)
    failures = []
    expected_failures = 0
    outcomes = {}
    try:
        family = socket.AF_INET6 if args.family == "ipv6" else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(TIMEOUT)
            for case in selected:
                protocol, case_name = case["protocol"], case["name"]
                if "wan-peer" in case.get("requires", []) and not args.peer_url:
                    reason = "requires wan-peer fixture (--peer-url or PCP_TEST_WAN_PEER=1)"
                    print(f"SKIP {protocol}/{case_name}: {reason}")
                    outcomes[(protocol, case_name)] = ("SKIP", reason)
                    continue
                unsupported = [item for item in case.get("requires", []) if item in args.unsupported]
                if unsupported:
                    reason = f"requires {unsupported[0]}, unsupported on this platform"
                    print(f"SKIP {protocol}/{case_name}: {reason}")
                    outcomes[(protocol, case_name)] = ("SKIP", reason)
                    continue
                if "daemon-restart" in case.get("requires", []) and not args.fixture:
                    reason = "requires daemon-restart fixture (--fixture)"
                    print(f"SKIP {protocol}/{case_name}: {reason}")
                    outcomes[(protocol, case_name)] = ("SKIP", reason)
                    continue
                blocked = []
                for dependency in case.get("depends_on", []):
                    dep_protocol, dep_name = dependency.split("/", 1)
                    dep_status = outcomes.get((dep_protocol, dep_name), ("SKIP", ""))[0]
                    if dep_status != "PASS":
                        blocked.append(f"{dependency} ({dep_status})")
                if blocked:
                    reason = "prerequisite did not pass: " + ", ".join(blocked)
                    print(f"SKIP {protocol}/{case_name}: {reason}")
                    outcomes[(protocol, case_name)] = ("SKIP", reason)
                    continue
                capture = start_packet_capture(args, case)
                try:
                    try:
                        run_case(sock, address, case, args.mutating, args.peer_url, args)
                    except (AssertionError, OSError) as error:
                        if case.get("known_failure"):
                            print(f"XFAIL {protocol}/{case_name}: {error} ({case['known_failure']})")
                            expected_failures += 1
                            outcomes[(protocol, case_name)] = (
                                "XFAIL", f"{error}; expected: {case['known_failure']}")
                            continue
                        print(f"FAIL {protocol}/{case_name}: {error}", file=sys.stderr)
                        failures.append((protocol, case_name))
                        outcomes[(protocol, case_name)] = ("FAIL", str(error))
                        continue
                    if case.get("known_failure"):
                        print(f"PASS {protocol}/{case_name}: configured RFC 7652 expectations satisfied")
                        outcomes[(protocol, case_name)] = (
                            "PASS", "Known-failure case met its expected behavior.")
                    else:
                        print(f"PASS {protocol}/{case_name}")
                        outcomes[(protocol, case_name)] = ("PASS", "")
                finally:
                    stop_packet_capture(capture)
    except KeyboardInterrupt:
        return 130
    selected_ids = {(case["protocol"], case["name"]) for case in selected}
    for case in cases:
        identity = (case["protocol"], case["name"])
        if identity in outcomes:
            continue
        if args.profile not in case.get("profiles", profiles):
            reason = "excluded by --profile filter"
        elif args.protocol != "both" and case["protocol"] not in (args.protocol, "both"):
            reason = "excluded by --protocol filter"
        elif args.family not in case["families"]:
            reason = "excluded by --family filter"
        elif args.case and case["name"] not in args.case:
            reason = "not selected by --case"
        elif case.get("mutating") and not args.mutating:
            reason = "requires --mutating"
        elif identity not in selected_ids:
            reason = "not selected for this invocation"
        else:
            reason = "not run"
        outcomes[identity] = ("SKIP", reason)
    try:
        write_status_report(args.status_file, args, sections, outcomes)
        print(f"Status report written to {args.status_file}")
    except OSError as error:
        print(f"Unable to write status report {args.status_file}: {error}", file=sys.stderr)
        if args.results_file or args.capture_dir:
            return 2
    results_file = args.results_file
    if not results_file and args.capture_dir:
        results_file = os.path.join(args.capture_dir, f"results-{args.family}.json")
    if results_file:
        try:
            write_results(results_file, args, cases, outcomes, corpus_path)
        except OSError as error:
            print(f"Unable to write results {results_file}: {error}", file=sys.stderr)
            return 2
    if failures:
        print(f"{len(failures)} of {len(selected)} cases failed.", file=sys.stderr)
        return 1
    if expected_failures:
        print(f"{expected_failures} expected failure(s); see the YAML case notes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
