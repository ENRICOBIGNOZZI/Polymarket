"""Public, immutable settlement observations for every decision market."""
import argparse
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request
from .catalog import LABEL
from .common import canonical, digest, immutable, read_json
from .dataset import validate_label


def collect(markets, output, *, maximum=100, opener=urllib.request.urlopen):
    output = Path(output); found = pending = 0; errors = []
    output.mkdir(parents=True, exist_ok=True)
    attempts_path = output/"attempts.json"
    attempts = read_json(attempts_path) if attempts_path.exists() else {}
    resolved = set()
    for path in output.glob('*.jsonl'):
        value = read_json(path)
        validate_label(value)
        resolved.add(str(value['market_id']))
    for market in sorted(set(map(str, markets))-resolved, key=lambda m: (attempts.get(m, 0), m))[:maximum]:
        url = "https://gamma-api.polymarket.com/markets/"+urllib.parse.quote(market, safe="")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "V7-causal-paper-research/1"})
            with opener(request, timeout=15) as response:
                raw = json.load(response)
            observed = time.time_ns()
            if raw.get("closed") is not True or raw.get("umaResolutionStatus") != "resolved":
                pending += 1; continue
            parse = lambda x: json.loads(x) if isinstance(x, str) else x
            tokens, payouts = parse(raw.get("clobTokenIds")), parse(raw.get("outcomePrices"))
            if not isinstance(tokens, list) or not isinstance(payouts, list):
                raise ValueError("SETTLEMENT_FIELDS_MISSING")
            value = {"schema": LABEL, "provider": "POLYMARKET_GAMMA_PUBLIC", "market_id": market,
                     "source_url": url, "closed": True, "resolution_status": "resolved",
                     "information_ns": observed, "payouts": dict(zip(tokens, map(float, payouts))),
                     "public_response": raw, "public_response_sha256": digest(canonical(raw))}
            validate_label(value)
            immutable(output/(digest(canonical(value))+".jsonl"), canonical(value)+b"\n"); found += 1
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append({"market_id": market, "reason": type(exc).__name__+":"+str(exc)})
        finally:
            attempts[market] = time.time_ns()
    temp = output/"attempts.tmp"
    temp.write_bytes(canonical(attempts)); temp.replace(attempts_path)
    return {"resolved": found, "pending": pending, "errors": errors,
            "information_time_semantics": "PUBLIC_RESPONSE_RECEIVED_NOW_NOT_BACKDATED_TO_MARKET_CLOSE"}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__); p.add_argument("--decisions", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True); a = p.parse_args()
    with a.decisions.open() as stream:
        markets = {r["market_id"] for line in stream if (r := json.loads(line)).get("outcome") is None}
    print(json.dumps(collect(markets, a.output), sort_keys=True))
