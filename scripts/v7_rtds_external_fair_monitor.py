#!/usr/bin/env python3
"""Observe public Polymarket RTDS inputs for the V7 External Fair PAPER plane.

The monitor itself has zero execution authority. It binds the contract,
captures the exact Chainlink reference, computes the fair interval and reports
the separately supervised PAPER router only after that router proves its safe
runtime contract.
"""
from __future__ import annotations

import argparse
import base64
from collections import deque
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
import re
import socket
import ssl
import struct
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from v7_adaptive_universe import normalize_market
from v7_public_https_proxy import DEFAULT_DNS, PublicResolver
from v7_contract_registry import contract_from_market
from v7_fair_value_registry import FairModelArtifact, RegistryError, SCHEMA_VERSION
from v7_external_fair_challenger import (
    RESIDUAL_FAMILY, predict_residual, validate_residual_parameters,
)
from v7_external_settlement_model import (
    FAMILY as SETTLEMENT_MODEL_FAMILY,
    predict as predict_settlement_model,
    runtime_features as settlement_runtime_features,
    validate_parameters as validate_settlement_parameters,
)

HOST = "ws-live-data.polymarket.com"
ORACLE_TOPIC = "crypto_prices_twap_sixty"
EXTERNAL_TOPIC = "crypto_prices"
ORACLE_WINDOW_SECONDS = 60
APPLICATION_HEARTBEAT_SECONDS = 5.0
RTDS_SILENCE_RECONNECT_SECONDS = 10.0
REFERENCE_MAX_GAP_MS = 2_000
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
FRESH_NS = 3_000_000_000
LATENCY_SAMPLE_LIMIT = 2_048
MALFORMED_FRAME_CAPTURE_BYTES = 4 * 1024


def load_registered_calibration(
    pointer_path: Path | None, *, code_sha: str, expected_role: str,
) -> tuple[FairModelArtifact | None, str]:
    """Load one immutable explicit pointer; filenames never grant authority."""
    if pointer_path is None:
        return None, "POINTER_NOT_CONFIGURED"
    pointer = load_json(pointer_path)
    if not pointer:
        return None, "POINTER_NOT_PUBLISHED"
    if (pointer.get("schema_version") != SCHEMA_VERSION
            or pointer.get("role") != expected_role
            or not re.fullmatch(r"[0-9a-f]{64}", str(pointer.get("model_hash") or ""))):
        return None, "POINTER_CONTRACT_INVALID"
    if expected_role == "CHAMPION" and not re.fullmatch(
        r"[0-9a-f]{64}", str(pointer.get("promotion_evidence_hash") or "")
    ):
        return None, "CHAMPION_PROMOTION_EVIDENCE_MISSING"
    artifact_raw = str(pointer.get("artifact") or "").strip()
    if not artifact_raw:
        return None, "ARTIFACT_PATH_MISSING"
    artifact_raw_path = Path(artifact_raw)
    artifact_path = artifact_raw_path if artifact_raw_path.is_absolute() \
        else (Path.cwd() / artifact_raw_path)
    raw = load_json(artifact_path.resolve())
    try:
        artifact = FairModelArtifact(**raw)
        artifact.validate()
        if artifact.family == RESIDUAL_FAMILY:
            if expected_role != "CHALLENGER":
                raise ValueError("residual_is_research_challenger_only")
            validate_residual_parameters(artifact)
            intercept, slope = 0.0, 1.0
        elif artifact.family == SETTLEMENT_MODEL_FAMILY:
            validate_settlement_parameters(artifact)
            intercept, slope = 0.0, 1.0
        else:
            intercept = float(artifact.parameters["calibration_intercept"])
            slope = float(artifact.parameters["calibration_slope"])
    except (KeyError, TypeError, ValueError, RegistryError):
        return None, "ARTIFACT_INVALID"
    if (artifact.model_hash != pointer["model_hash"]
            or artifact.model_version != pointer.get("model_version")
            or artifact.code_sha != code_sha
            or not math.isfinite(intercept)
            or not math.isfinite(slope)
            or not 0.05 <= slope <= 5.0):
        return None, "ARTIFACT_IDENTITY_OR_PARAMETERS_INVALID"
    return artifact, "LOADED"


def calibrated_probability(probability: float, artifact: FairModelArtifact) -> float:
    p = min(1.0 - 1e-9, max(1e-9, float(probability)))
    intercept = float(artifact.parameters["calibration_intercept"])
    slope = float(artifact.parameters["calibration_slope"])
    value = intercept + slope * math.log(p / (1.0 - p))
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def latency_quantiles(samples: deque[float] | list[float]) -> dict[str, float]:
    """Return bounded empirical latency quantiles without external dependencies."""
    ordered = sorted(value for value in samples if math.isfinite(value) and value >= 0.0)
    if not ordered:
        return {}

    def percentile(probability: float) -> float:
        index = probability * (len(ordered) - 1)
        lower = int(math.floor(index))
        upper = int(math.ceil(index))
        if lower == upper:
            return ordered[lower]
        weight = index - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    return {
        "p50": percentile(0.50),
        "p90": percentile(0.90),
        "p99": percentile(0.99),
        "max": ordered[-1],
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        output.flush()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def router_live_market_yes(
    router: dict[str, Any],
    *,
    code_sha: str,
    market_id: str,
    now_ms: int,
) -> float | None:
    live = router.get("live_market") if isinstance(router.get("live_market"), dict) else {}
    try:
        value = float(live.get("yes"))
        age_ms = now_ms - int(live.get("receive_ts_ms") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        router.get("code_sha") != code_sha
        or live.get("valid") is not True
        or live.get("source") != "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH"
        or str(live.get("market_id") or "") != market_id
        or not math.isfinite(value) or not 0.0 <= value <= 1.0
        or age_ms < -250 or age_ms > 5_000
    ):
        return None
    return value


def fetch_market_by_slug(gamma_url: str, slug: str, timeout: int = 4) -> dict[str, Any] | None:
    """Fetch one exact Gamma market without general-universe eligibility filters."""
    query = urllib.parse.urlencode({"slug": slug})
    request = urllib.request.Request(
        gamma_url.rstrip("/") + "/markets?" + query,
        headers={"User-Agent": "polymarket-v7-external-fair/1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    rows = value if isinstance(value, list) else value.get("markets", []) if isinstance(value, dict) else []
    for raw in rows:
        if not isinstance(raw, dict) or str(raw.get("slug") or "") != slug:
            continue
        market = normalize_market(raw)
        if market is not None:
            return market
    return None


def _recv_exact(stream: ssl.SSLSocket, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        chunk = stream.recv(size - len(output))
        if not chunk:
            raise OSError("websocket EOF")
        output.extend(chunk)
    return bytes(output)


def connect_websocket(resolver: PublicResolver) -> ssl.SSLSocket:
    last_error: Exception | None = None
    for address in resolver.resolve(HOST):
        raw: socket.socket | None = None
        try:
            raw = socket.create_connection((address, 443), timeout=8.0)
            stream = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET / HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\nOrigin: https://polymarket.com\r\n\r\n"
            )
            stream.sendall(request.encode("ascii"))
            response = bytearray()
            while b"\r\n\r\n" not in response:
                response.extend(stream.recv(4096))
                if len(response) > 65536:
                    raise OSError("oversized websocket handshake")
            header, _, remainder = bytes(response).partition(b"\r\n\r\n")
            if not header.startswith(b"HTTP/1.1 101 ") or remainder:
                raise OSError("websocket upgrade rejected")
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()).decode("ascii")
            headers = {}
            for line in header.decode("latin-1").split("\r\n")[1:]:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            if headers.get("sec-websocket-accept") != expected:
                raise OSError("websocket accept mismatch")
            stream.settimeout(1.0)
            return stream
        except Exception as exc:  # noqa: BLE001 - try all public addresses
            last_error = exc
            if raw is not None:
                try:
                    raw.close()
                except OSError:
                    pass
    raise OSError(f"RTDS connection failed: {last_error}")


def send_frame(stream: ssl.SSLSocket, opcode: int, payload: bytes) -> None:
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("websocket payload too large")
    first = 0x80 | (opcode & 0x0F)
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = bytes((first, 0x80 | length))
    elif length <= 0xFFFF:
        header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
    else:
        header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    stream.sendall(header + mask + masked)


def send_json(stream: ssl.SSLSocket, payload: dict[str, Any]) -> None:
    send_frame(stream, 0x1, json.dumps(payload, separators=(",", ":")).encode())


def read_frame(stream: ssl.SSLSocket) -> tuple[bool, int, bytes]:
    first, second = _recv_exact(stream, 2)
    if first & 0x70:
        raise OSError("unsupported websocket extension")
    final, opcode = bool(first & 0x80), first & 0x0F
    masked, length = bool(second & 0x80), second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(stream, 8))[0]
    if length > MAX_MESSAGE_BYTES:
        raise OSError("websocket message exceeds bound")
    mask = _recv_exact(stream, 4) if masked else b""
    payload = _recv_exact(stream, length)
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return final, opcode, payload


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_ms(raw: Any) -> int:
    try:
        value = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return 0
    if value > 10**17:
        return value // 1_000_000
    if value > 10**14:
        return value // 1_000
    if value < 10**11:
        return value * 1_000
    return value


def _oracle_decimal(value: dict[str, Any]) -> str | None:
    raw = value.get("full_accuracy_value")
    if raw is not None:
        try:
            return format(Decimal(str(raw)) / Decimal(10**18), "f")
        except (InvalidOperation, TypeError, ValueError):
            return None
    raw = value.get("value", value.get("price"))
    try:
        number = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return format(number, "f") if number.is_finite() and number > 0 else None


def boundary_reference(
    history: dict[int, dict[str, Any]], boundary_ms: int, *, max_gap_ms: int = REFERENCE_MAX_GAP_MS
) -> dict[str, Any] | None:
    eligible = [timestamp for timestamp in history if timestamp <= boundary_ms]
    if not eligible:
        return None
    timestamp = max(eligible)
    if boundary_ms - timestamp > max_gap_ms:
        return None
    return history[timestamp]


def rtds_stream_silent(last_observation_monotonic: float, now_monotonic: float) -> bool:
    return now_monotonic - last_observation_monotonic >= RTDS_SILENCE_RECONNECT_SECONDS


def observations(value: Any, inherited_topic: str = "") -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        for row in value:
            yield from observations(row, inherited_topic)
        return
    if not isinstance(value, dict):
        return
    topic = str(value.get("topic") or inherited_topic)
    payload = value.get("payload")
    if isinstance(payload, (list, dict)):
        yield from observations(payload, topic)
        return
    if topic not in {ORACLE_TOPIC, EXTERNAL_TOPIC}:
        return
    price = _finite(value.get("value", value.get("price")))
    timestamp_ms = _timestamp_ms(value.get("timestamp", value.get("timestamp_ms")))
    symbol = str(value.get("symbol") or "").lower()
    if topic == ORACLE_TOPIC:
        try:
            window_seconds = int(value.get("window_s", value.get("windowSeconds")))
        except (TypeError, ValueError, OverflowError):
            return
        if window_seconds != ORACLE_WINDOW_SECONDS:
            return
    if price is None or price <= 0.0 or timestamp_ms <= 0:
        return
    row = {"topic": topic, "symbol": symbol, "price": price, "timestamp_ms": timestamp_ms}
    if topic == ORACLE_TOPIC:
        row["window_seconds"] = ORACLE_WINDOW_SECONDS
        price_decimal = _oracle_decimal(value)
        if price_decimal is None:
            return
        row["price_decimal"] = price_decimal
    yield row


PAPER_BOOTSTRAP_SCHEMA = "polymarket_v7_paper_exploration_bootstrap_v1"


def validate_paper_bootstrap_policy(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate an explicit PAPER-only mechanistic bootstrap policy.

    This policy is not a trained model, receives no promotion credit and can
    never grant authenticated or real-money authority. An immutable registered
    champion always takes precedence when one exists.
    """
    if raw is None or raw.get("enabled") is not True:
        return None
    required_identity = {
        "schema": PAPER_BOOTSTRAP_SCHEMA,
        "model_id": "btc_m5_same_oracle_diffusion_bootstrap_v1",
        "asset": "BTC",
        "horizon": "M5",
        "contract_template": "BTC_USD_UPDOWN_5M",
        "uses_polymarket_price_as_feature": False,
        "promotion_credit": False,
        "real_money_authority": False,
    }
    if any(raw.get(key) != value for key, value in required_identity.items()):
        raise ValueError("paper bootstrap identity or authority contract invalid")
    numeric_bounds = {
        "minimum_fresh_venue_count": (3.0, 8.0),
        "minimum_tte_seconds": (1.0, 30.0),
        "maximum_tte_seconds": (60.0, 300.0),
        "minimum_innovation_bps_per_sqrt_second": (0.5, 5.0),
        "maximum_innovation_bps_per_sqrt_second": (2.0, 20.0),
        "dispersion_innovation_multiplier": (0.0, 5.0),
        "minimum_sigma_bps": (5.0, 50.0),
        "maximum_sigma_bps": (50.0, 300.0),
        "mean_uncertainty_floor_bps": (2.0, 30.0),
        "mean_uncertainty_sigma_fraction": (0.1, 1.0),
        "mean_uncertainty_dispersion_multiplier": (0.0, 10.0),
        "confidence_z": (1.28, 2.58),
        "maximum_absolute_mean_margin_bps": (50.0, 500.0),
    }
    policy = dict(raw)
    for key, (minimum, maximum) in numeric_bounds.items():
        value = _finite(policy.get(key))
        if value is None or not minimum <= value <= maximum:
            raise ValueError(f"paper bootstrap parameter invalid:{key}")
        policy[key] = value
    policy["minimum_fresh_venue_count"] = int(policy["minimum_fresh_venue_count"])
    if policy["maximum_tte_seconds"] <= policy["minimum_tte_seconds"]:
        raise ValueError("paper bootstrap tte interval invalid")
    if policy["maximum_innovation_bps_per_sqrt_second"] < policy["minimum_innovation_bps_per_sqrt_second"]:
        raise ValueError("paper bootstrap innovation bounds invalid")
    if policy["maximum_sigma_bps"] < policy["minimum_sigma_bps"]:
        raise ValueError("paper bootstrap sigma bounds invalid")
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    policy["policy_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return policy


def _normal_cdf(value: float) -> float:
    return 0.5 * math.erfc(-value / math.sqrt(2.0))


def paper_bootstrap_prediction(
    features: dict[str, Any], external: dict[str, Any], policy: dict[str, Any],
) -> dict[str, float] | None:
    """Mechanistic same-oracle probability for bounded PAPER exploration.

    The mean is the external composite's current margin over the exact
    Chainlink opening reference, blended toward the already observed oracle
    margin during the final sixty-second TWAP window. Uncertainty is explicitly
    conservative and increases with remaining time, short-horizon innovations
    and cross-venue dispersion.
    """
    if external.get("valid") is not True or int(external.get("fresh_venue_count") or 0) < int(
        policy["minimum_fresh_venue_count"]
    ):
        return None
    tte = _finite(features.get("tte_seconds"))
    observed = _finite(features.get("terminal_window_observed_fraction"))
    oracle_margin = _finite(features.get("oracle_minus_reference_bps"))
    external_minus_oracle = _finite(features.get("external_minus_oracle_bps"))
    return_1s = _finite(features.get("external_return_1s"))
    return_5s = _finite(features.get("external_return_5s"))
    dispersion = _finite(external.get("dispersion_bps"))
    if None in (tte, observed, oracle_margin, external_minus_oracle, return_1s, return_5s, dispersion):
        return None
    assert tte is not None and observed is not None and oracle_margin is not None
    assert external_minus_oracle is not None and return_1s is not None and return_5s is not None
    assert dispersion is not None
    if not policy["minimum_tte_seconds"] <= tte <= policy["maximum_tte_seconds"]:
        return None
    if not 0.0 <= observed <= 1.0 or dispersion < 0.0 or dispersion > 50.0:
        return None

    external_reference_margin = oracle_margin + external_minus_oracle
    mean_margin = observed * oracle_margin + (1.0 - observed) * external_reference_margin
    maximum_mean = float(policy["maximum_absolute_mean_margin_bps"])
    mean_margin = min(maximum_mean, max(-maximum_mean, mean_margin))

    one_second_innovation = abs(return_1s) * 10_000.0
    five_second_innovation = abs(return_5s) * 10_000.0 / math.sqrt(5.0)
    dispersion_innovation = dispersion * float(policy["dispersion_innovation_multiplier"])
    innovation = max(
        float(policy["minimum_innovation_bps_per_sqrt_second"]),
        one_second_innovation,
        five_second_innovation,
        dispersion_innovation,
    )
    innovation = min(float(policy["maximum_innovation_bps_per_sqrt_second"]), innovation)

    # The observed fraction of the terminal TWAP is already locked. Preserve a
    # positive uncertainty floor instead of pretending the end state is known.
    effective_seconds = max(1.0, tte * max(0.20, 1.0 - 0.80 * observed))
    sigma = innovation * math.sqrt(effective_seconds)
    sigma = min(float(policy["maximum_sigma_bps"]), max(float(policy["minimum_sigma_bps"]), sigma))
    mean_uncertainty = max(
        float(policy["mean_uncertainty_floor_bps"]),
        sigma * float(policy["mean_uncertainty_sigma_fraction"]),
        dispersion * float(policy["mean_uncertainty_dispersion_multiplier"]),
    )
    confidence_z = float(policy["confidence_z"])
    yes = min(1.0 - 1e-9, max(1e-9, _normal_cdf(mean_margin / sigma)))
    lower = min(yes, _normal_cdf((mean_margin - confidence_z * mean_uncertainty) / sigma))
    upper = max(yes, _normal_cdf((mean_margin + confidence_z * mean_uncertainty) / sigma))
    return {
        "yes": yes,
        "lower": max(1e-9, lower),
        "upper": min(1.0 - 1e-9, upper),
        "raw_yes": yes,
        "predicted_settlement_margin_bps": mean_margin,
        "settlement_sigma_bps": sigma,
        "mean_uncertainty_bps": mean_uncertainty,
        "innovation_bps_per_sqrt_second": innovation,
    }


class Monitor:
    def __init__(self, root: Path, code_sha: str, *, universe_path: Path | None = None,
                 approvals_path: Path | None = None, external_venues_path: Path | None = None,
                 champion_pointer: Path | None = None,
                 challenger_pointer: Path | None = None,
                 paper_bootstrap: dict[str, Any] | None = None,
                 gamma_url: str = "https://gamma-api.polymarket.com") -> None:
        self.root, self.code_sha = root, code_sha
        self.universe_path = universe_path
        self.external_venues_path = external_venues_path
        self.gamma_url = gamma_url.rstrip("/")
        self.latest: dict[str, dict[str, Any]] = {}
        self.latency_samples: dict[str, deque[float]] = {
            "chainlink_source_to_receive": deque(maxlen=LATENCY_SAMPLE_LIMIT),
            "binance_source_to_receive": deque(maxlen=LATENCY_SAMPLE_LIMIT),
            "fair_compute": deque(maxlen=LATENCY_SAMPLE_LIMIT),
        }
        self.oracle_history: dict[int, dict[str, Any]] = {}
        self.accepted = self.written = self.dropped = self.duplicates = 0
        self.empty_frames = self.malformed_frames = 0
        self.last_malformed_frame: dict[str, Any] = {}
        self.connection_epoch = self.reconnects = self.gaps = 0
        self.last_error = "starting"
        self.last_contract_refresh_ns = 0
        self.active_market: dict[str, Any] = {}
        self.active_contract: dict[str, Any] = {}
        self.reference: dict[str, Any] = {}
        self.champion, self.champion_load_state = load_registered_calibration(
            champion_pointer, code_sha=code_sha, expected_role="CHAMPION")
        self.challenger, self.challenger_load_state = load_registered_calibration(
            challenger_pointer, code_sha=code_sha, expected_role="CHALLENGER")
        self.paper_bootstrap = validate_paper_bootstrap_policy(paper_bootstrap)
        self.approved_rule_hashes: set[str] = set()
        if approvals_path is not None:
            raw = json.loads(approvals_path.read_text(encoding="utf-8"))
            if raw.get("schema") != "polymarket_v7_external_fair_rule_approvals_v1" or raw.get("paper_only") is not True:
                raise ValueError("external fair rule approval contract invalid")
            approvals = raw.get("approved_rule_hashes")
            self.approved_rule_hashes = set(approvals) if isinstance(approvals, dict) else set()

    def ingest(self, row: dict[str, Any]) -> None:
        topic = str(row["topic"])
        previous = self.latest.get(topic)
        sequence = int(row["timestamp_ms"])
        if topic == ORACLE_TOPIC and sequence in self.oracle_history:
            # RTDS can repeat the current TWAP observation.  It is a valid
            # duplicate, not a parser failure or an unwritten tape event.
            self.duplicates += 1
            return
        receive_wall_ns = time.time_ns()
        enriched = dict(row)
        enriched.update({
            "schema": "polymarket_v7_rtds_price_event_v1",
            "connection_epoch": self.connection_epoch,
            "receive_wall_ns": receive_wall_ns,
            "receive_monotonic_ns": time.monotonic_ns(),
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
        })
        stage = (
            "chainlink_source_to_receive"
            if topic == ORACLE_TOPIC
            else "binance_source_to_receive"
        )
        self.latency_samples[stage].append(
            max(0.0, receive_wall_ns / 1_000_000.0 - sequence)
        )
        if topic == ORACLE_TOPIC:
            self.oracle_history[sequence] = enriched
            floor = sequence - 20 * 60 * 1000
            for timestamp in [value for value in self.oracle_history if value < floor]:
                del self.oracle_history[timestamp]
        if previous is None or sequence > int(previous["timestamp_ms"]):
            self.latest[topic] = enriched
        self.accepted += 1
        append_jsonl(self.root / "rtds_events.jsonl", enriched)
        self.written += 1

    def record_malformed_frame(self, payload: bytes, error: str) -> None:
        """Retain bounded public-wire evidence without treating it as tape loss."""
        self.malformed_frames += 1
        captured = payload[:MALFORMED_FRAME_CAPTURE_BYTES]
        record = {
            "schema": "polymarket_v7_rtds_rejected_frame_v1",
            "receive_wall_ns": time.time_ns(),
            "connection_epoch": self.connection_epoch,
            "error": error,
            "payload_bytes": len(payload),
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "captured_payload_base64": base64.b64encode(captured).decode("ascii"),
            "payload_truncated": len(captured) != len(payload),
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
        }
        self.last_malformed_frame = {
            key: record[key] for key in (
                "connection_epoch", "error", "payload_bytes", "payload_sha256", "payload_truncated"
            )
        }
        append_jsonl(self.root / "rtds_rejected_frames.jsonl", record)

    def record_empty_frame(self) -> None:
        """RTDS emits an empty text-frame keepalive after subscribing."""
        self.empty_frames += 1

    def refresh_contract(self, now_ns: int) -> None:
        if self.universe_path is None or now_ns - self.last_contract_refresh_ns < 15_000_000_000:
            return
        self.last_contract_refresh_ns = now_ns
        try:
            universe = json.loads(self.universe_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.active_market = self.active_contract = self.reference = {}
            return
        now_seconds = now_ns // 1_000_000_000
        candidates: list[tuple[int, dict[str, Any]]] = []
        for market in universe.get("markets") if isinstance(universe.get("markets"), list) else []:
            if not isinstance(market, dict):
                continue
            match = re.fullmatch(r"btc-updown-5m-([0-9]+)", str(market.get("slug") or ""))
            if match is None:
                continue
            start = int(match.group(1))
            if start <= now_seconds < start + 300 and market.get("accepting_orders") is True:
                candidates.append((start, market))
        if not candidates:
            # The adaptive universe is flow-eligible and can remove a still-
            # open 5m contract after its volume falls below the general floor.
            # Preserve a verified binding through its contractual window so a
            # metadata refresh cannot erase causal settlement state.
            active_start = int(self.active_market.get("contract_start_epoch") or 0)
            if (active_start <= now_seconds < active_start + 300
                    and self.active_contract.get("verified_template") is True
                    and self.active_contract.get("rules_hash_recognized") is True):
                return
            # At rollover the specialized contract may not yet be in the
            # general universe. Discover its deterministic slug directly;
            # transport/schema/contract failures remain fail-closed.
            current_start = now_seconds - now_seconds % 300
            try:
                targeted = fetch_market_by_slug(
                    self.gamma_url, f"btc-updown-5m-{current_start}"
                )
            except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
                targeted = None
            if (targeted is not None and targeted.get("active") is True
                    and targeted.get("closed") is not True
                    and targeted.get("accepting_orders") is True):
                candidates.append((current_start, targeted))
        if not candidates:
            self.active_market = self.active_contract = self.reference = {}
            return
        start, market = max(candidates, key=lambda value: value[0])
        spec = contract_from_market(market, approved_rule_hashes=self.approved_rule_hashes)
        self.active_market = dict(market)
        self.active_market["contract_start_epoch"] = start
        self.active_contract = dict(spec.__dict__)
        self.active_contract["verification_reasons"] = list(spec.verification_reasons)
        boundary_ms = start * 1000
        reference_event = boundary_reference(self.oracle_history, boundary_ms)
        observation_ms = int(reference_event.get("timestamp_ms") or 0) if reference_event else 0
        gap_ms = boundary_ms - observation_ms if reference_event else 0
        self.reference = {
            "valid": bool(reference_event and spec.informed_trading_authorized),
            "value": float(reference_event.get("price") or 0.0) if reference_event else 0.0,
            "exact_value": str(reference_event.get("price_decimal") or "") if reference_event else "",
            "version": start,
            "contract_boundary_timestamp_ms": boundary_ms,
            "observation_timestamp_ms": observation_ms,
            "boundary_gap_ms": gap_ms,
            "boundary_fallback": bool(reference_event and gap_ms > 0),
            "receive_monotonic_ns": int(reference_event.get("receive_monotonic_ns") or 0) if reference_event else 0,
            "provenance": (
                "Polymarket public RTDS Chainlink BTC/USD 60-second TWAP "
                + ("exact contract-boundary observation" if reference_event and gap_ms == 0 else
                   "latest causal observation within 2 seconds before contract boundary")
            ),
        }
        atomic_json(self.root / "contract_registry.json", {
            "schema": "polymarket_v7_live_contract_registry_v1", "code_sha": self.code_sha,
            "paper_only": True, "approved_rule_hashes": sorted(self.approved_rule_hashes),
            "active_contract": self.active_contract, "settlement_reference": self.reference,
        })

    def external_snapshot(self, now_ns: int) -> dict[str, Any]:
        if self.external_venues_path is None:
            return {}
        try:
            value = json.loads(self.external_venues_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        age_ns = max(0, now_ns - int(value.get("timestamp_ns") or 0))
        if value.get("code_sha") != self.code_sha or age_ns > FRESH_NS:
            return {}
        value["age_ns"] = age_ns
        return value

    def fair_snapshot(self, now_ns: int, oracle_healthy: bool,
                      external: dict[str, Any], market_yes: float | None = None) -> dict[str, Any]:
        market, contract, reference = self.active_market, self.active_contract, self.reference
        start = int(market.get("contract_start_epoch") or 0)
        tte = max(0.0, start + 300 - now_ns / 1_000_000_000.0) if start else 0.0
        gamma_mid = float(market.get("midpoint") or 0.5)
        base = {"valid": False, "yes": 0.5, "lower": 0.0, "upper": 1.0,
                "structural": 0.5, "calibrated": 0.5, "pm_mid": market_yes,
                "structural_lower": 0.0, "structural_upper": 1.0,
                "calibration_state": self.champion_load_state,
                "probability_model_id": "settlement_model_unavailable",
                "probability_model_hash": "",
                "explicit_champion_applied": False,
                "inference_state": "IMMUTABLE_SETTLEMENT_MODEL_REQUIRED",
                "model_features": None,
                "pm_mid_source": (
                    "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH" if market_yes is not None
                    else "UNAVAILABLE"
                ),
                "gamma_discovery_mid_diagnostic": gamma_mid,
                "tte_seconds": tte, "calculated_monotonic_ns": time.monotonic_ns(),
                "valid_until_monotonic_ns": 0}
        oracle = self.latest.get(ORACLE_TOPIC, {})
        if not (contract.get("verified_template") and contract.get("rules_hash_recognized")
                and reference.get("valid") and oracle_healthy and external.get("valid")
                and int(external.get("fresh_venue_count") or 0) >= 2 and tte > 0.0):
            return base
        oracle_price = float(oracle["price"])
        efficient = float(external.get("composite_price") or 0.0)
        reference_price = float(reference["value"])
        dispersion_bps = max(0.0, float(external.get("dispersion_bps") or 0.0))
        if min(oracle_price, efficient, reference_price) <= 0.0 or dispersion_bps > 50.0:
            return base
        features = settlement_runtime_features(
            tte_seconds=tte, reference_price=reference_price,
            oracle_price=oracle_price, external=external,
            oracle_age_ns=max(0, now_ns - int(oracle["receive_wall_ns"])),
        )
        base["model_features"] = features
        if features is None:
            base["inference_state"] = "SETTLEMENT_MODEL_FEATURES_INCOMPLETE"
            return base
        valid_until = time.monotonic_ns() + min(
            max(0, FRESH_NS - (now_ns - int(oracle["receive_wall_ns"]))),
            max(0, FRESH_NS - int(external.get("age_ns") or 0)),
        )
        champion = self.champion
        rule_hash = str(contract.get("normalized_rules_hash") or "")
        champion_scope_valid = bool(
            champion is not None
            and champion.family == SETTLEMENT_MODEL_FAMILY
            and "BTC" in champion.assets
            and "BTC_USD_UPDOWN_5M" in champion.contract_templates
            and rule_hash in champion.rules_hashes
        )
        if champion_scope_valid and champion is not None:
            try:
                prediction = predict_settlement_model(champion, features)
            except ValueError:
                base["inference_state"] = "SETTLEMENT_MODEL_INFERENCE_FAILED"
                return base
            return {**base, "valid": True, "yes": prediction["yes"],
                    "lower": prediction["lower"], "upper": prediction["upper"],
                    "structural_lower": prediction["lower"],
                    "structural_upper": prediction["upper"],
                    "structural": prediction["raw_yes"],
                    "calibrated": prediction["yes"],
                    "calibration_state": "IMMUTABLE_SETTLEMENT_CHAMPION_APPLIED",
                    "inference_state": "VALID",
                    "probability_model_id": champion.model_version,
                    "probability_model_hash": champion.model_hash,
                    "explicit_champion_applied": True,
                    "paper_exploration_bootstrap": False,
                    "promotion_eligible": True,
                    "settlement_margin_bps": prediction["predicted_settlement_margin_bps"],
                    "settlement_sigma_bps": prediction["settlement_sigma_bps"],
                    "settlement_mean_uncertainty_bps": prediction["mean_uncertainty_bps"],
                    "calculated_monotonic_ns": time.monotonic_ns(),
                    "valid_until_monotonic_ns": valid_until}

        if champion is not None:
            base["calibration_state"] = "EXPLICIT_CHAMPION_SCOPE_OR_FAMILY_MISMATCH"
            return base
        if self.paper_bootstrap is None:
            base["calibration_state"] = self.champion_load_state
            return base
        bootstrap_prediction = paper_bootstrap_prediction(features, external, self.paper_bootstrap)
        if bootstrap_prediction is None:
            base["calibration_state"] = "PAPER_EXPLORATION_BOOTSTRAP_REJECTED"
            base["inference_state"] = "PAPER_BOOTSTRAP_INPUTS_OUTSIDE_CONTRACT"
            return base
        return {**base, "valid": True,
                "yes": bootstrap_prediction["yes"],
                "lower": bootstrap_prediction["lower"],
                "upper": bootstrap_prediction["upper"],
                "structural_lower": bootstrap_prediction["lower"],
                "structural_upper": bootstrap_prediction["upper"],
                "structural": bootstrap_prediction["raw_yes"],
                "calibrated": bootstrap_prediction["yes"],
                "calibration_state": "PAPER_EXPLORATION_BOOTSTRAP_APPLIED",
                "inference_state": "VALID_PAPER_EXPLORATION_BOOTSTRAP",
                "probability_model_id": self.paper_bootstrap["model_id"],
                "probability_model_hash": self.paper_bootstrap["policy_hash"],
                "explicit_champion_applied": False,
                "paper_exploration_bootstrap": True,
                "promotion_eligible": False,
                "real_money_authority": False,
                "settlement_margin_bps": bootstrap_prediction["predicted_settlement_margin_bps"],
                "settlement_sigma_bps": bootstrap_prediction["settlement_sigma_bps"],
                "settlement_mean_uncertainty_bps": bootstrap_prediction["mean_uncertainty_bps"],
                "bootstrap_innovation_bps_per_sqrt_second": bootstrap_prediction[
                    "innovation_bps_per_sqrt_second"],
                "calculated_monotonic_ns": time.monotonic_ns(),
                "valid_until_monotonic_ns": valid_until}

    def registered_shadow_snapshot(
        self, external_only: dict[str, Any], artifact: FairModelArtifact | None,
        load_state: str, role: str,
    ) -> dict[str, Any]:
        output = dict(external_only)
        output.update({
            "authority": "SHADOW",
            "registry_role": role,
            "model_id": f"{role.lower()}_calibration",
            "registry_load_state": load_state,
            "explicit_registry_model_applied": False,
        })
        if artifact is None:
            return output
        rules_hash = str(self.active_contract.get("normalized_rules_hash") or "")
        if ("BTC" not in artifact.assets
                or "BTC_USD_UPDOWN_5M" not in artifact.contract_templates
                or rules_hash not in artifact.rules_hashes):
            output["registry_load_state"] = "SCOPE_MISMATCH"
            return output
        if artifact.family == RESIDUAL_FAMILY:
            # Point-probability research, never a validated risk interval or champion.
            forward_start = int(artifact.hyperparameters.get("forward_oos_starts_after_ns") or 0)
            start_ns = int(self.active_market.get("contract_start_epoch") or 0) * 1_000_000_000
            features = external_only.get("model_features")
            if (role != "CHALLENGER" or external_only.get("valid") is not True
                    or not isinstance(features, dict) or start_ns < forward_start):
                output.update(valid=False, registry_load_state="AWAITING_FORWARD_CONTRACT_OR_INPUTS")
                return output
            try:
                probability = predict_residual(artifact, float(external_only["yes"]), features)
            except (KeyError, TypeError, ValueError):
                output.update(valid=False, registry_load_state="RESIDUAL_INFERENCE_INVALID")
                return output
            output.update({
                "yes": probability, "calibrated": probability, "lower": 0.0, "upper": 1.0,
                "probability_interval_validated": False, "promotion_eligible": False,
                "probability_model_id": artifact.model_version, "probability_model_hash": artifact.model_hash,
                "explicit_registry_model_applied": True, "uses_polymarket_price_as_feature": False,
                "forward_start_ns": forward_start, "frozen_at_ns": artifact.generated_timestamp_ns,
                "family": RESIDUAL_FAMILY, "execution_authority": "SHADOW_ZERO_AUTHORITY",
            })
            return output
        if artifact.family == SETTLEMENT_MODEL_FAMILY:
            features = external_only.get("model_features")
            if not isinstance(features, dict):
                output.update({"valid": False, "registry_load_state": "FEATURES_INCOMPLETE"})
                return output
            try:
                prediction = predict_settlement_model(artifact, features)
            except ValueError:
                output.update({"valid": False, "registry_load_state": "INFERENCE_FAILED"})
                return output
            output.update({
                "valid": True,
                "yes": prediction["yes"], "calibrated": prediction["yes"],
                "lower": prediction["lower"], "upper": prediction["upper"],
                "structural": prediction["raw_yes"],
                "structural_lower": prediction["lower"],
                "structural_upper": prediction["upper"],
                "probability_model_id": artifact.model_version,
                "probability_model_hash": artifact.model_hash,
                "explicit_registry_model_applied": True,
                "settlement_margin_bps": prediction["predicted_settlement_margin_bps"],
                "settlement_sigma_bps": prediction["settlement_sigma_bps"],
            })
            return output
        if external_only.get("valid") is not True:
            return output
        structural = float(external_only["structural"])
        lower = calibrated_probability(float(external_only["structural_lower"]), artifact)
        upper = calibrated_probability(float(external_only["structural_upper"]), artifact)
        probability = calibrated_probability(structural, artifact)
        output.update({
            "yes": probability,
            "calibrated": probability,
            "lower": lower,
            "upper": upper,
            "probability_model_id": artifact.model_version,
            "probability_model_hash": artifact.model_hash,
            "explicit_registry_model_applied": True,
        })
        return output

    def hybrid_fair_snapshot(self, external_only: dict[str, Any]) -> dict[str, Any]:
        """Build the separate market-prior cohort without changing the external benchmark.

        This cohort is deliberately shadow-only and immature.  The bounded logit blend is
        a declared baseline for measuring whether Polymarket state adds incremental value;
        it is not silently substituted for the external-only execution benchmark.
        """
        output = dict(external_only)
        output.update({
            "model_id": "hybrid_fair", "authority": "SHADOW",
            "uses_polymarket_price_as_feature": True,
        })
        if external_only.get("valid") is not True:
            return output
        try:
            market_raw = float(external_only.get("pm_mid"))
        except (TypeError, ValueError, OverflowError):
            output.update({"valid": False, "reason": "LIVE_CLOB_BENCHMARK_UNAVAILABLE"})
            return output
        if not math.isfinite(market_raw) or not 0.0 <= market_raw <= 1.0:
            output.update({"valid": False, "reason": "LIVE_CLOB_BENCHMARK_UNAVAILABLE"})
            return output
        external_yes = min(1.0 - 1e-9, max(1e-9, float(external_only["yes"])))
        market_yes = min(1.0 - 1e-9, max(1e-9, market_raw))
        weight = 0.35
        external_logit = math.log(external_yes / (1.0 - external_yes))
        market_logit = math.log(market_yes / (1.0 - market_yes))
        hybrid_logit = external_logit + weight * (market_logit - external_logit)
        hybrid_yes = 1.0 / (1.0 + math.exp(-hybrid_logit))
        half_width = max(
            hybrid_yes - float(external_only["lower"]),
            float(external_only["upper"]) - hybrid_yes,
        )
        output.update({
            "yes": hybrid_yes,
            "calibrated": hybrid_yes,
            "lower": max(0.0, hybrid_yes - half_width),
            "upper": min(1.0, hybrid_yes + half_width),
            "market_prior_logit_weight": weight,
            "external_only_yes": external_yes,
        })
        return output

    def publish(self) -> None:
        now = time.time_ns()
        self.refresh_contract(now)
        oracle, external = self.latest.get(ORACLE_TOPIC, {}), self.latest.get(EXTERNAL_TOPIC, {})
        oracle_age = max(0, now - int(oracle.get("receive_wall_ns") or 0)) if oracle else 0
        external_age = max(0, now - int(external.get("receive_wall_ns") or 0)) if external else 0
        oracle_healthy = bool(oracle and oracle_age <= FRESH_NS)
        external_fresh = bool(external and external_age <= FRESH_NS)
        venue_runtime = self.external_snapshot(now)
        multi_venue_healthy = bool(venue_runtime.get("valid") and int(venue_runtime.get("fresh_venue_count") or 0) >= 2)
        continuity = "LIVE_CONTINUOUS" if oracle_healthy and self.accepted >= 2 else "CONTINUITY_UNKNOWN"
        router = load_json(self.root / "paper_router_status.json")
        market_id = str(self.active_market.get("market_id") or "")
        live_market_yes = router_live_market_yes(
            router, code_sha=self.code_sha, market_id=market_id,
            now_ms=now // 1_000_000,
        )
        fair_started = time.monotonic_ns()
        fair = self.fair_snapshot(now, oracle_healthy, venue_runtime, live_market_yes)
        fair["model_id"] = "external_only_fair"
        fair["authority"] = "SHADOW"
        fair["uses_polymarket_price_as_feature"] = False
        hybrid_fair = self.hybrid_fair_snapshot(fair)
        challenger_fair = self.registered_shadow_snapshot(
            fair, self.challenger, self.challenger_load_state, "CHALLENGER")
        self.latency_samples["fair_compute"].append(
            max(0.0, (time.monotonic_ns() - fair_started) / 1_000_000.0)
        )
        common = {"paper_only": True, "authenticated_execution": False, "real_order_submission": False}
        shadow_collector_active = bool(
            router.get("schema") == "polymarket_v7_crypto_settlement_engine_status_v1"
            and router.get("code_sha") == self.code_sha
            and router.get("state") == "RUNNING"
            and router.get("paper_only") is True
            and router.get("authenticated_execution") is False
            and router.get("real_order_submission") is False
            and router.get("execution_authority") == "OPPORTUNITY_PROPOSAL_ONLY"
            and router.get("capital_authority") is False
            and router.get("oms_authority") is False
            and router.get("inventory_authority") is False
            and router.get("ledger_writer_authority") is False
            and router.get("order_submission_enabled") is False
            and router.get("counterfactual_collection_enabled") is True
            and router.get("killed") is False
            and not router.get("blocker")
            and int(time.time()) - int(router.get("timestamp") or 0) <= 5
        )
        router_maturity = router.get("maturity") if isinstance(
            router.get("maturity"), dict) else {}
        maker_quote_authority_eligible = bool(
            fair.get("explicit_champion_applied") is True
            and router.get("model_mature") is True
            and router_maturity.get("eligible_for_manual_paper_promotion") is True
            and float(router_maturity.get("virtual_2x_cost_stress_pnl") or 0.0) > 0.0
        )
        atomic_json(self.root / "oracle_status.json", {
            "schema": "polymarket_v7_same_oracle_status_v1", "state": continuity,
            "reason": "" if oracle_healthy else self.last_error, "timestamp_ns": now,
            "healthy": oracle_healthy, "value": float(oracle.get("price") or 0.0),
            "age_ns": oracle_age, "source_sequence": int(oracle.get("timestamp_ms") or 0),
            "connection_epoch": self.connection_epoch, "reconnects": self.reconnects,
            "gaps": self.gaps, "transport": "POLYMARKET_PUBLIC_RTDS_CHAINLINK", **common,
        })
        status = {
            "schema": "polymarket_v7_external_fair_status_v1",
            "state": ("FULL_FAIR_SHADOW_OPERATIONAL" if fair.get("valid") and shadow_collector_active else
                      "FAIR_WITHOUT_COLLECTOR" if fair.get("valid") else
                      "DATA_PLANE_OPERATIONAL" if oracle_healthy else "DATA_PLANE_DEGRADED"),
            "code_sha": self.code_sha, "execution_authority": (
                "SHADOW_ZERO_AUTHORITY"
            ),
            "external_fair_required_markets": 1 if self.active_contract else 0, **common,
            "market": {
                "market_id": str(self.active_market.get("market_id") or ""),
                "event_id": str((self.active_market.get("event_ids") or [""])[0]),
                "slug": str(self.active_market.get("slug") or ""),
                "yes_token": str((self.active_market.get("clob_token_ids") or ["", ""])[0]),
                "no_token": str((self.active_market.get("clob_token_ids") or ["", ""])[1]),
                "best_bid": float(self.active_market.get("best_bid") or 0.0),
                "best_ask": float(self.active_market.get("best_ask") or 0.0),
                "fee_schedule": self.active_market.get("fee_schedule") or {},
            },
            "contract": {"verified": bool(self.active_contract.get("verified_template")),
                         "rules_hash_recognized": bool(self.active_contract.get("rules_hash_recognized")),
                         "rules_hash": str(self.active_contract.get("normalized_rules_hash") or ""),
                         "oracle_window_seconds": int(self.active_contract.get("oracle_window_seconds") or 60)},
            "settlement_reference": self.reference or {"valid": False, "value": 0.0, "version": 0},
            "oracle": {"healthy": oracle_healthy, "value": float(oracle.get("price") or 0.0),
                       "age_ns": oracle_age, "continuity": continuity,
                       "connection_epoch": self.connection_epoch, "reconnects": self.reconnects, "gaps": self.gaps},
            "external": {"healthy": multi_venue_healthy,
                         "fresh_venue_count": int(venue_runtime.get("fresh_venue_count") or (1 if external_fresh else 0)),
                         "dispersion_bps": float(venue_runtime.get("dispersion_bps") or 0.0),
                         "age_ns": int(venue_runtime.get("age_ns") or external_age),
                         "composite_price": float(venue_runtime.get("composite_price") or external.get("price") or 0.0),
                         "composite_microprice": float(venue_runtime.get("composite_microprice") or external.get("price") or 0.0),
                         "return_250ms": venue_runtime.get("return_250ms"),
                         "return_1s": venue_runtime.get("return_1s"),
                         "return_5s": venue_runtime.get("return_5s"),
                         "return_30s": venue_runtime.get("return_30s"),
                         "realized_vol_fast": venue_runtime.get("realized_vol_fast"),
                         "realized_vol_medium": venue_runtime.get("realized_vol_medium"),
                         "realized_vol_slow": venue_runtime.get("realized_vol_slow"),
                         "realized_vol_30s": venue_runtime.get("realized_vol_30s"),
                         "aggregate_ofi": venue_runtime.get("aggregate_ofi"),
                         "aggregate_trade_imbalance": venue_runtime.get("aggregate_trade_imbalance"),
                         "venues": [{
                "venue": "VENUE_COMPOSITE" if multi_venue_healthy else "BINANCE_SPOT",
                "healthy": multi_venue_healthy or external_fresh,
                "age_ns": int(venue_runtime.get("age_ns") or external_age),
                "price": float(venue_runtime.get("composite_price") or external.get("price") or 0.0),
                "microprice": float(venue_runtime.get("composite_microprice") or external.get("price") or 0.0),
                "spread_bps": 0.0, "weight": 1.0, "basis_bps": 0.0,
                "disabled": not (multi_venue_healthy or external_fresh)}]},
            "fair": fair,
            "fair_models": {
                "external_only_fair": fair,
                "hybrid_fair": hybrid_fair,
                "registered_challenger": challenger_fair,
                "execution_model_id": fair.get("probability_model_id"),
                "comparison_state": (
                    "LIVE_SHADOW_COMPARISON" if hybrid_fair.get("valid") is True
                    else "AWAITING_LIVE_CLOB_BENCHMARK"
                ),
            },
            "model": {
                "mature": maker_quote_authority_eligible,
                "champion_load_state": self.champion_load_state,
                "challenger_load_state": self.challenger_load_state,
                "explicit_champion_applied": fair.get(
                    "explicit_champion_applied") is True,
                "maker_quote_authority_eligible": maker_quote_authority_eligible,
                "manual_model_maturity_flag": router.get("model_mature") is True,
                "positive_2x_cost_stress": float(
                    router_maturity.get("virtual_2x_cost_stress_pnl") or 0.0) > 0.0,
                "probability_interval_bin_consistency": float(
                    (router_maturity.get("probability_interval_diagnostics") or {}).get(
                        "consistency_rate") or 0.0),
                "probability_interval_eligible_bins": int(
                    (router_maturity.get("probability_interval_diagnostics") or {}).get(
                        "eligible_bin_count") or 0),
                "economic_confidence": router.get(
                    "economic_confidence", "MORE_EVIDENCE_REQUIRED"),
            },
            "latency": {
                stage: latency_quantiles(samples)
                for stage, samples in self.latency_samples.items()
                if samples
            },
            "actions": router.get("actions") if shadow_collector_active else {},
            "counterfactual_actions": (
                router.get("counterfactual_actions") if shadow_collector_active else {}
            ),
            "economics": {
                "realized_pnl": float(router.get("realized_pnl") or 0.0),
                "counterfactual_realized_pnl": float(
                    router.get("counterfactual_realized_pnl") or 0.0
                ),
                "counterfactual_equity": float(router.get("counterfactual_equity") or 0.0),
            },
            "paper_router": router,
            "tape": {"evidence_valid": True, "accepted": self.accepted,
                     "written": self.written, "dropped": self.dropped,
                     "duplicates": self.duplicates,
                     "empty_frames": self.empty_frames,
                     "malformed_frames": self.malformed_frames,
                     "last_malformed_frame": self.last_malformed_frame},
            "blockers": (["CONTRACT_BINDING_NOT_RUNNING"] if not self.active_contract else [])
                        + (["CONTRACT_RULES_NOT_AUTHORIZED"] if self.active_contract and not self.active_contract.get("rules_hash_recognized") else [])
                        + (["SETTLEMENT_REFERENCE_NOT_CAPTURED"] if not self.reference.get("valid") else [])
                        + (["MULTI_VENUE_EXTERNAL_COMPOSITE_NOT_RUNNING"] if not multi_venue_healthy else [])
                        + (["FAIR_VALUE_INVALID"] if not fair.get("valid") else [])
                        + ([] if shadow_collector_active else ["COUNTERFACTUAL_COLLECTOR_NOT_RUNNING"])
                        + ([str(router.get("blocker"))] if router.get("blocker") else []),
        }
        atomic_json(self.root / "status.json", status)

    def run(self, resolver: PublicResolver) -> None:
        while True:
            stream: ssl.SSLSocket | None = None
            try:
                self.connection_epoch += 1
                if self.connection_epoch > 1:
                    self.reconnects += 1
                    self.gaps += 1
                stream = connect_websocket(resolver)
                send_json(stream, {"action": "subscribe", "subscriptions": [
                    {"topic": ORACLE_TOPIC, "type": "update", "filters": "{\"symbol\":\"btc/usd\"}"},
                    {"topic": EXTERNAL_TOPIC, "type": "*", "filters": "{\"symbol\":\"BTCUSDT\"}"},
                ]})
                fragments, fragment_opcode = bytearray(), 0
                last_application_heartbeat = time.monotonic()
                last_observation_monotonic = last_application_heartbeat
                self.last_error = "awaiting_public_rtds_observation"
                self.publish()
                while True:
                    now_monotonic = time.monotonic()
                    if rtds_stream_silent(last_observation_monotonic, now_monotonic):
                        raise TimeoutError("RTDS price observation silence exceeded reconnect threshold")
                    if now_monotonic - last_application_heartbeat >= APPLICATION_HEARTBEAT_SECONDS:
                        send_frame(stream, 0x1, b"PING")
                        last_application_heartbeat = now_monotonic
                    try:
                        final, opcode, payload = read_frame(stream)
                    except socket.timeout:
                        self.publish()
                        continue
                    if opcode == 0x8:
                        raise OSError("RTDS websocket closed")
                    if opcode == 0x9:
                        send_frame(stream, 0xA, payload)
                        continue
                    if opcode == 0xA:
                        continue
                    if opcode in {0x1, 0x2}:
                        fragments, fragment_opcode = bytearray(payload), opcode
                    elif opcode == 0x0 and fragment_opcode:
                        fragments.extend(payload)
                    else:
                        raise OSError("unexpected websocket opcode")
                    if len(fragments) > MAX_MESSAGE_BYTES:
                        raise OSError("fragmented websocket message exceeds bound")
                    if not final:
                        continue
                    if fragment_opcode == 0x1:
                        if not fragments:
                            self.record_empty_frame()
                        else:
                            try:
                                decoded = json.loads(fragments.decode("utf-8"))
                            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                                # An unparseable wire frame contains no qualified
                                # observation.  Keep it distinct from a dropped
                                # accepted tape event so evidence loss remains
                                # auditable.
                                self.record_malformed_frame(
                                    bytes(fragments),
                                    "invalid_utf8" if isinstance(exc, UnicodeDecodeError) else "invalid_json",
                                )
                            else:
                                observed = False
                                for row in observations(decoded):
                                    self.ingest(row)
                                    observed = True
                                if observed:
                                    last_observation_monotonic = time.monotonic()
                    fragments, fragment_opcode = bytearray(), 0
                    self.last_error = ""
                    self.publish()
            except Exception as exc:  # noqa: BLE001 - persistent reconnect loop
                self.last_error = f"rtds_transport:{type(exc).__name__}:{exc}"
                self.publish()
                time.sleep(min(5.0, 0.25 * (2 ** min(self.reconnects, 4))))
            finally:
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--universe", type=Path)
    parser.add_argument("--approvals", type=Path)
    parser.add_argument("--external-venues", type=Path)
    parser.add_argument("--champion-pointer", type=Path)
    parser.add_argument("--challenger-pointer", type=Path)
    parser.add_argument("--external-fair-config", type=Path)
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--dns", action="append", default=[])
    args = parser.parse_args()
    if len(args.code_sha) != 40 or any(char not in "0123456789abcdef" for char in args.code_sha):
        raise SystemExit("--code-sha must be a lowercase 40-character Git SHA")
    external_config = load_json(args.external_fair_config) if args.external_fair_config else {}
    Monitor(args.output_dir.resolve(), args.code_sha, universe_path=args.universe,
            approvals_path=args.approvals, external_venues_path=args.external_venues,
            champion_pointer=args.champion_pointer,
            challenger_pointer=args.challenger_pointer,
            paper_bootstrap=external_config.get("paper_exploration_bootstrap")
            if isinstance(external_config.get("paper_exploration_bootstrap"), dict) else None,
            gamma_url=args.gamma_url).run(
        PublicResolver(args.dns or list(DEFAULT_DNS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
