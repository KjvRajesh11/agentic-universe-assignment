"""Unit tests for schema loader, disposition service, and bucket classification."""

import pytest

from solution.bucket_service import BucketService
from solution.disposition_service import DispositionService
from solution.schema_loader import SchemaLoader


@pytest.fixture
def schema_loader():
    return SchemaLoader("assignment-a-data/schemas")


def test_schema_loader_workflows(schema_loader):
    schemas = schema_loader.get_all_schemas()
    assert "motor_renewal" in schemas
    assert "policy_feedback" in schemas

    motor = schema_loader.get_schema("motor_renewal")
    assert motor.workflow_id == "motor_renewal"
    assert motor.reference_field.name == "policy_number"

    # Check PII fields
    pii_fields = [f.name for f in motor.fields if f.pii]
    assert "customer_name" in pii_fields
    assert "phone" in pii_fields

    feedback = schema_loader.get_schema("policy_feedback")
    assert feedback.reference_field.name == "order_id"


def test_dispositions_catalogue(schema_loader):
    disp_service = DispositionService(schema_loader)

    # Valid dispositions
    assert disp_service.is_valid_code("motor_renewal", "renewed") is True
    assert disp_service.is_valid_code("motor_renewal", "dnd") is True
    assert disp_service.is_valid_code("policy_feedback", "feedback_captured") is True

    # Invalid dispositions
    assert disp_service.is_valid_code("motor_renewal", "feedback_captured") is False
    assert disp_service.is_valid_code("motor_renewal", "unknown_code") is False

    renewed = disp_service.get_disposition("motor_renewal", "renewed")
    assert renewed.is_terminal is True
    assert renewed.is_success is True

    callback = disp_service.get_disposition("motor_renewal", "callback_requested")
    assert callback.is_terminal is False
    assert callback.is_success is False


def test_effective_disposition_resolution(schema_loader):
    disp_service = DispositionService(schema_loader)
    wf = "motor_renewal"

    # 1. No disposition events
    events1 = [
        {"event_type": "call.initiated", "occurred_at_epoch": 100.0},
        {"event_type": "call.connected", "occurred_at_epoch": 105.0},
    ]
    code, disp = disp_service.resolve_effective_disposition(wf, events1)
    assert code is None
    assert disp is None

    # 2. Only disposition.set
    events2 = [
        {"event_type": "call.initiated", "occurred_at_epoch": 100.0},
        {
            "event_type": "disposition.set",
            "occurred_at_epoch": 120.0,
            "payload": {"disposition_code": "payment_link_sent"},
        },
    ]
    code, disp = disp_service.resolve_effective_disposition(wf, events2)
    assert code == "payment_link_sent"
    assert disp.is_success is False

    # 3. disposition.set followed by disposition.override
    events3 = [
        {
            "event_type": "disposition.set",
            "occurred_at_epoch": 120.0,
            "payload": {"disposition_code": "not_interested"},
        },
        {
            "event_type": "disposition.override",
            "occurred_at_epoch": 200.0,
            "payload": {"disposition_code": "renewed"},
        },
    ]
    code, disp = disp_service.resolve_effective_disposition(wf, events3)
    assert code == "renewed"
    assert disp.is_success is True
    assert disp.is_terminal is True

    # 4. Multiple overrides: latest by occurred_at wins
    events4 = [
        {
            "event_type": "disposition.set",
            "occurred_at_epoch": 100.0,
            "payload": {"disposition_code": "callback_requested"},
        },
        {
            "event_type": "disposition.override",
            "occurred_at_epoch": 150.0,
            "payload": {"disposition_code": "renewed"},
        },
        {
            "event_type": "disposition.override",
            "occurred_at_epoch": 250.0,
            "payload": {"disposition_code": "dnd"},
        },
    ]
    code, disp = disp_service.resolve_effective_disposition(wf, events4)
    assert code == "dnd"
    assert disp.is_terminal is True
    assert disp.is_success is False


def test_bucket_precedence():
    # 1. yet_to_reach_out: no calls
    assert (
        BucketService.classify_lead_bucket(
            total_calls=0, has_terminal_call=False, every_call_telephony_failed=False
        )
        == BucketService.YET_TO_REACH_OUT
    )

    # 2. completed: any terminal call
    assert (
        BucketService.classify_lead_bucket(
            total_calls=1, has_terminal_call=True, every_call_telephony_failed=False
        )
        == BucketService.COMPLETED
    )

    # 3. in_progress_telephony_failed: every call failed
    assert (
        BucketService.classify_lead_bucket(
            total_calls=2, has_terminal_call=False, every_call_telephony_failed=True
        )
        == BucketService.IN_PROGRESS_TELEPHONY_FAILED
    )

    # Mixed calls: one telephony failure, one connected/non-terminal call
    assert (
        BucketService.classify_lead_bucket(
            total_calls=2, has_terminal_call=False, every_call_telephony_failed=False
        )
        == BucketService.IN_PROGRESS_DIALED
    )
