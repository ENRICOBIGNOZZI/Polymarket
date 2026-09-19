from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = r"""
#include "pm/v7_clob_order_salt.hpp"
#include <cassert>
#include <cstdint>
#include <limits>
#include <unordered_set>
using namespace pm::v7::clob_order;
int main(){
  OrderSaltSequence invalid(0); assert(!invalid.valid()); assert(invalid.next()==0);
  OrderSaltSequence seq(42); assert(seq.valid());
  for(std::uint64_t x=42;x<1042;++x) assert(seq.next()==x);

  OrderSaltSequence wrap(std::numeric_limits<std::uint64_t>::max()-1);
  assert(wrap.next()==std::numeric_limits<std::uint64_t>::max()-1);
  assert(wrap.next()==std::numeric_limits<std::uint64_t>::max());
  assert(wrap.next()==1);
  assert(wrap.next()==2);

  auto os=OrderSaltSequence::from_os_entropy(); assert(os.valid());
  std::unordered_set<std::uint64_t> seen;
  for(int i=0;i<10000;++i){auto v=os.next();assert(v!=0);assert(seen.insert(v).second);}
  return 0;
}
"""


def test_native_order_salt_sequence() -> None:
    compiler=shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp); main=p/"main.cpp"; binary=p/"salt-test"; main.write_text(PROGRAM)
        subprocess.run([compiler,"-std=c++20","-O2","-Wall","-Wextra","-Wpedantic",
                        f"-I{ROOT/'include'}",str(ROOT/'src/v7_clob_order_salt.cpp'),
                        str(main),"-o",str(binary)],check=True,capture_output=True,text=True)
        subprocess.run([str(binary)],check=True,timeout=10)


if __name__ == "__main__":
    test_native_order_salt_sequence()
