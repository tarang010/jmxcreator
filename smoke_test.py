from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET

from core.traffic_capture import CapturedRequest
from core.transaction_grouper import group_into_transactions
from core.correlation_engine import run_correlation
from core.jmx_generator import generate_jmx, generate_csv


def run_smoke_test() -> None:
    reqs = [
        CapturedRequest(
            sequence=1,
            url="https://example.com/api/login",
            method="POST",
            headers={"content-type": "application/json"},
            body='{"username":"demo_user","password":"secret123"}',
            response_status=200,
            response_headers={"content-type": "application/json"},
            response_body='{"access_token":"abc123456789token"}',
            timestamp=0.0,
            page_context="Login",
            resource_type="xhr",
        ),
        CapturedRequest(
            sequence=2,
            url="https://example.com/api/profile",
            method="GET",
            headers={"authorization": "Bearer abc123456789token"},
            body=None,
            response_status=200,
            response_headers={"content-type": "application/json"},
            response_body='{"ok":true}',
            timestamp=0.1,
            page_context="Profile",
            resource_type="xhr",
        ),
    ]

    txs, summary = group_into_transactions(reqs, app_name="Example")
    corrs, csv_cols = run_correlation(reqs)

    with tempfile.TemporaryDirectory() as td:
        jmx_path = os.path.join(td, "smoke.jmx")
        csv_path = os.path.join(td, "smoke.csv")

        generate_jmx(
            app_name="Example",
            transactions=txs,
            correlations=corrs,
            csv_columns=csv_cols,
            user_journey_summary=summary,
            output_path=jmx_path,
        )

        if csv_cols:
            generate_csv(csv_cols, csv_path)

        tree = ET.parse(jmx_path)
        root = tree.getroot()
        assert root.tag == "jmeterTestPlan", "Invalid root element"
        assert root.find("hashTree") is not None, "Missing top-level hashTree"
        xml_text = open(jmx_path, "r", encoding="utf-8").read()
        assert "HTTPSamplerProxy" in xml_text, "No sampler found"
        assert "ThreadGroup" in xml_text, "No ThreadGroup found"

    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    run_smoke_test()
