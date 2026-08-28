// Canonical V7 Fast Structural PAPER runtime.
//
// The implementation is split into bounded translation units fragments to keep
// the hot-path reviewable.  This file is the sole compilation owner; no legacy
// executable or alternate OMS is created.
#include "fast_runtime/part1.inc"
#include "fast_runtime/part2.inc"
#include "fast_runtime/part3.inc"
#include "fast_runtime/part4.inc"
