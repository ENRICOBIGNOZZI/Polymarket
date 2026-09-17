from __future__ import annotations

import math
import random
import shutil
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUNDING = {
    1000: (1, 2, 3),
    100: (2, 2, 4),
    50: (3, 2, 5),
    25: (4, 2, 6),
    10: (3, 2, 5),
    1: (4, 2, 6),
}


def decimal_places(x: float) -> int:
    return abs(Decimal(x.__str__()).as_tuple().exponent)


def round_down(x: float, digits: int) -> float:
    return math.floor(x * (10**digits)) / (10**digits)


def round_normal(x: float, digits: int) -> float:
    return round(x * (10**digits)) / (10**digits)


def round_up(x: float, digits: int) -> float:
    return math.ceil(x * (10**digits)) / (10**digits)


def to_token_decimals(x: float) -> int:
    f = 1_000_000 * x
    if decimal_places(f) > 0:
        f = round_normal(f, 0)
    return int(f)


def official_limit(side: int, quantity_micro: int, price_e4: int, tick_e4: int) -> tuple[int, int]:
    price_digits, size_digits, amount_digits = ROUNDING[tick_e4]
    size = quantity_micro / 1_000_000
    price = price_e4 / 10_000
    raw_price = round_normal(price, price_digits)
    if side == 1:
        taker = round_down(size, size_digits)
        maker = taker * raw_price
        if decimal_places(maker) > amount_digits:
            maker = round_up(maker, amount_digits + 4)
            if decimal_places(maker) > amount_digits:
                maker = round_down(maker, amount_digits)
        return to_token_decimals(maker), to_token_decimals(taker)
    maker = round_down(size, size_digits)
    taker = maker * raw_price
    if decimal_places(taker) > amount_digits:
        taker = round_up(taker, amount_digits + 4)
        if decimal_places(taker) > amount_digits:
            taker = round_down(taker, amount_digits)
    return to_token_decimals(maker), to_token_decimals(taker)


def test_native_amounts_match_official_v2_limit_builder() -> None:
    rng = random.Random(20260917)
    cases: list[tuple[int, int, int, int, int, int]] = []
    for tick in ROUNDING:
        for side in (1, 2):
            for _ in range(120):
                price_e4 = rng.randint(1, 9999 // tick) * tick
                quantity = rng.randint(1, 2_000_000_000)
                maker, taker = official_limit(side, quantity, price_e4, tick)
                cases.append((side, price_e4, tick, quantity, maker, taker))

    rows = ",\n".join(
        f"{{{s},{p},{t},{q}LL,{m}LL,{k}LL}}" for s, p, t, q, m, k in cases
    )
    program = f'''\
#include "pm/v7_clob_order_amounts.hpp"
#include <limits>
using namespace pm::v7;
using namespace pm::v7::clob_order;
struct C {{ int side, price, tick; long long qty, maker, taker; }};
int main() {{
  const C cases[] = {{ {rows} }};
  for (const auto& c : cases) {{
    auto out = marketable_limit_amounts(c.side==1?Side::Buy:Side::Sell,c.price,c.tick,c.qty);
    if (!out.valid || out.maker_amount!=c.maker || out.taker_amount!=c.taker) return 2;
  }}
  if (marketable_limit_amounts(Side::Buy, 5001, 100, 5000000).valid) return 3;
  if (marketable_limit_amounts(Side::Buy, 5000, 3, 5000000).valid) return 4;
  if (marketable_limit_amounts(Side::Buy, 5000, 100, 9999).valid) return 5;
  if (marketable_limit_amounts(Side::Buy, 9900, 100,
      std::numeric_limits<long long>::max()).valid) return 6;
  for (int tick : {{1000,100,50,25,10,1}}) {{
    if (!supported_exchange_v2_tick_e4(tick)) return 7;
  }}
  if (supported_exchange_v2_tick_e4(3)) return 8;
  return 0;
}}
'''
    compiler = shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        main = p / "main.cpp"
        binary = p / "amount-test"
        main.write_text(program)
        subprocess.run(
            [compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
             f"-I{ROOT/'include'}", str(ROOT/'src/v7_clob_order_amounts.cpp'),
             str(main), "-o", str(binary)],
            check=True, capture_output=True, text=True,
        )
        subprocess.run([str(binary)], check=True, timeout=10)
