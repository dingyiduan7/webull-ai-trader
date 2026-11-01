import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.research.sec_edgar import (
    EdgarConfig,
    EdgarError,
    EdgarHttp,
    next_regular_session,
    select_snapshot,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "sec_edgar"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def inputs():
    return (
        load("company_tickers_exchange.json"),
        load("submissions.json"),
        load("companyfacts.json"),
    )


def snapshot(at):
    tickers, submissions, facts = inputs()
    return select_snapshot(
        "QUAL",
        "NYSE",
        tickers,
        submissions,
        facts,
        as_of=at,
        retrieved_at=datetime(2026, 11, 4, 18, tzinfo=timezone.utc),
        calendar=reviewed_calendar(),
    )


def test_network_is_opt_in_and_requires_owner_configured_identity():
    with pytest.raises(EdgarError, match="SEC_USER_AGENT_REQUIRED"):
        EdgarConfig("", "", enabled=True)

    class MustNotRun:
        def __call__(self):
            raise AssertionError("network session created while disabled")

    with pytest.raises(EdgarError, match="SEC_NETWORK_NOT_ENABLED"):
        EdgarHttp(EdgarConfig("", ""), session_factory=MustNotRun()).get_json(
            "https://data.sec.gov/submissions/CIK0000000001.json"
        )


def test_http_uses_only_configured_user_agent_and_official_https_hosts():
    observed = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def get(self, url, **kwargs):
            observed.update(url=url, **kwargs)
            return Response()

    client = EdgarHttp(
        EdgarConfig("Example Research", "owner@example.test", enabled=True),
        session_factory=Session,
    )
    assert client.get_json("https://data.sec.gov/submissions/CIK0000000001.json") == {"ok": True}
    assert observed["headers"]["User-Agent"] == "Example Research owner@example.test"
    assert observed["allow_redirects"] is False
    with pytest.raises(EdgarError, match="SEC_ENDPOINT_REJECTED"):
        client.get_json("https://example.com/data.json")


def test_filing_is_unavailable_until_next_regular_session():
    calendar = reviewed_calendar()
    assert next_regular_session("2026-11-02T21:00:00Z", calendar) == datetime(
        2026, 11, 3, 14, 30, tzinfo=timezone.utc
    )
    before = snapshot(datetime(2026, 11, 3, 14, 29, 59, tzinfo=timezone.utc))
    after = snapshot(datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc))
    assert (before.fiscal_period, before.accession) == ("Q2", "0000000001-26-000002")
    assert (after.fiscal_period, after.accession) == ("Q3", "0000000001-26-000003")


@pytest.mark.parametrize(
    ("at", "period", "expected", "component_accessions"),
    [
        (
            datetime(2026, 8, 4, 14, 30, tzinfo=timezone.utc),
            "Q2",
            Decimal("18"),
            ("0000000001-26-000002", "0000000001-26-000001"),
        ),
        (
            datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc),
            "Q3",
            Decimal("25"),
            ("0000000001-26-000003", "0000000001-26-000002"),
        ),
    ],
)
def test_quarter_cash_flow_is_derived_from_cumulative_ytd(at, period, expected, component_accessions):
    result = snapshot(at)
    cash_flow = result.facts["operating_cash_flow"]
    assert result.fiscal_period == period
    assert cash_flow.value == expected
    assert tuple(item.accession for item in cash_flow.components) == component_accessions
    assert cash_flow.start == cash_flow.components[1].end + timedelta(days=1)
    assert cash_flow.raw_provenance["derivation"] == "current cumulative less prior cumulative"


def test_normalized_fact_preserves_point_in_time_and_raw_provenance():
    result = snapshot(datetime(2026, 11, 4, 18, tzinfo=timezone.utc))
    revenue = result.facts["revenue"]
    assert result.issuer.cik == "0000000001"
    assert result.issuer.exchange == "NYSE"
    assert revenue.value == Decimal("150")
    assert revenue.unit == "USD"
    assert revenue.tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert revenue.accepted_at == datetime(2026, 11, 2, 21, tzinfo=timezone.utc)
    assert revenue.available_at == datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc)
    assert revenue.components[0].raw_provenance["company_fact"]["val"] == 150
    assert revenue.components[0].raw_provenance["submission"]["accessionNumber"] == result.accession


def test_ambiguous_unit_fails_closed():
    tickers, submissions, facts = inputs()
    concept = facts["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]
    concept["units"]["EUR"] = [copy.deepcopy(concept["units"]["USD"][-1])]
    with pytest.raises(EdgarError, match="SEC_FACT_UNIT_AMBIGUOUS"):
        select_snapshot("QUAL", "NYSE", tickers, submissions, facts,
            as_of=datetime(2026, 11, 4, 18, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 11, 4, 18, tzinfo=timezone.utc), calendar=reviewed_calendar())


def test_ambiguous_standard_tags_fail_closed():
    tickers, submissions, facts = inputs()
    source = facts["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]
    facts["facts"]["us-gaap"]["Revenues"] = copy.deepcopy(source)
    with pytest.raises(EdgarError, match="SEC_FACT_TAG_AMBIGUOUS"):
        select_snapshot("QUAL", "NYSE", tickers, submissions, facts,
            as_of=datetime(2026, 11, 4, 18, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 11, 4, 18, tzinfo=timezone.utc), calendar=reviewed_calendar())


@pytest.mark.parametrize("source", ["submissions", "companyfacts"])
def test_cik_mismatch_fails_closed(source):
    tickers, submissions, facts = inputs()
    (submissions if source == "submissions" else facts)["cik"] = "2"
    with pytest.raises(EdgarError, match="SEC_CIK_MISMATCH"):
        select_snapshot("QUAL", "NYSE", tickers, submissions, facts,
            as_of=datetime(2026, 11, 4, 18, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 11, 4, 18, tzinfo=timezone.utc), calendar=reviewed_calendar())


def test_missing_required_fact_is_not_replaced_with_zero():
    tickers, submissions, facts = inputs()
    del facts["facts"]["us-gaap"]["OperatingIncomeLoss"]
    with pytest.raises(EdgarError, match="SEC_REQUIRED_FACT_MISSING:OPERATING_INCOME"):
        select_snapshot("QUAL", "NYSE", tickers, submissions, facts,
            as_of=datetime(2026, 11, 4, 18, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 11, 4, 18, tzinfo=timezone.utc), calendar=reviewed_calendar())


def test_incompatible_fiscal_period_fails_closed():
    tickers, submissions, facts = inputs()
    fact = facts["facts"]["us-gaap"]["OperatingIncomeLoss"]["units"]["USD"][-1]
    fact["fp"] = "Q2"
    with pytest.raises(EdgarError, match="SEC_FACT_PERIOD_MISMATCH"):
        select_snapshot("QUAL", "NYSE", tickers, submissions, facts,
            as_of=datetime(2026, 11, 4, 18, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 11, 4, 18, tzinfo=timezone.utc), calendar=reviewed_calendar())


def test_custom_taxonomy_amendment_and_stale_facts_fail_closed():
    tickers, submissions, facts = inputs()
    custom = copy.deepcopy(facts)
    custom["facts"]["qual"] = {}
    with pytest.raises(EdgarError, match="SEC_CUSTOM_TAXONOMY_UNSUPPORTED"):
        select_snapshot("QUAL", "NYSE", tickers, submissions, custom,
            as_of=datetime(2026, 11, 4, 18, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 11, 4, 18, tzinfo=timezone.utc), calendar=reviewed_calendar())

    amended = copy.deepcopy(submissions)
    for key, value in {
        "accessionNumber": "0000000001-26-000004", "filingDate": "2026-11-04",
        "reportDate": "2026-09-30", "acceptanceDateTime": "2026-11-04T21:00:00Z", "form": "10-Q/A",
    }.items():
        amended["filings"]["recent"][key].insert(0, value)
    with pytest.raises(EdgarError, match="SEC_AMENDMENT_UNSUPPORTED"):
        select_snapshot("QUAL", "NYSE", tickers, amended, facts,
            as_of=datetime(2026, 11, 6, 16, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 11, 6, 16, tzinfo=timezone.utc), calendar=reviewed_calendar())

    with pytest.raises(EdgarError, match="SEC_FACTS_STALE"):
        select_snapshot("QUAL", "NYSE", tickers, submissions, facts,
            as_of=datetime(2026, 12, 30, 18, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 12, 30, 18, tzinfo=timezone.utc), calendar=reviewed_calendar(),
            maximum_fact_age=timedelta(days=30))
