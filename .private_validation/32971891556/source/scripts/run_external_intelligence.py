#!/usr/bin/env python3
from __future__ import annotations

import external_intelligence
from external_request_policy import wrap_request_json
from gdelt_webngrams import wrap_collect_gdelt


external_intelligence.request_json = wrap_request_json(external_intelligence.request_json)
external_intelligence.collect_gdelt = wrap_collect_gdelt(
    external_intelligence,
    external_intelligence.collect_gdelt,
)


if __name__ == "__main__":
    raise SystemExit(external_intelligence.main())
