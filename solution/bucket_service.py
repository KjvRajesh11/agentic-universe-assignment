"""Lead bucket classification service following exact precedence rules."""

from __future__ import annotations


class BucketService:
    """Classifies a lead into one of four mutually exclusive lifecycle buckets."""

    YET_TO_REACH_OUT = "yet_to_reach_out"
    COMPLETED = "completed"
    IN_PROGRESS_TELEPHONY_FAILED = "in_progress_telephony_failed"
    IN_PROGRESS_DIALED = "in_progress_dialed"

    @classmethod
    def classify_lead_bucket(
        cls,
        total_calls: int,
        has_terminal_call: bool,
        every_call_telephony_failed: bool,
    ) -> str:
        """
        Classify bucket according to strict specification precedence:
        1. yet_to_reach_out: no calls.
        2. completed: any call's effective disposition is terminal (is_terminal=true).
        3. in_progress_telephony_failed: every call has a call.telephony_failed event.
        4. in_progress_dialed: all remaining cases.
        """
        if total_calls == 0:
            return cls.YET_TO_REACH_OUT

        if has_terminal_call:
            return cls.COMPLETED

        if every_call_telephony_failed:
            return cls.IN_PROGRESS_TELEPHONY_FAILED

        return cls.IN_PROGRESS_DIALED
