#!/usr/bin/env python3
from __future__ import annotations

import external_intelligence
from external_request_policy import wrap_request_json


external_intelligence.request_json = wrap_request_json(external_intelligence.request_json)


if __name__ == "__main__":
    raise SystemExit(external_intelligence.main())
