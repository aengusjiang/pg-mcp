"""Unit tests for observability wiring: metrics reset and request-id filter."""

import logging

import pytest

from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import (
    RequestIdFilter,
    clear_request_id,
    request_context,
    set_request_id,
)


class TestMetricsReset:
    """reset_all_metrics must be repeatable (no duplicate registration)."""

    def test_reset_twice_does_not_raise(self) -> None:
        """Repeated resets do not hit Duplicated timeseries errors."""
        collector = MetricsCollector()
        collector.increment_query_request("success", "db1")
        collector.reset_all_metrics()
        collector.reset_all_metrics()

    def test_counters_usable_after_reset(self) -> None:
        """Counters work normally after a reset."""
        collector = MetricsCollector()
        collector.reset_all_metrics()
        collector.increment_query_request("success", "db1")
        collector.observe_query_duration(0.5)

    def test_observe_query_duration(self) -> None:
        """observe_query_duration records into the histogram without labels."""
        collector = MetricsCollector()
        collector.observe_query_duration(0.25)


class TestRequestIdFilter:
    """RequestIdFilter injects the contextvar request_id into records."""

    def _make_logger(self) -> tuple[logging.Logger, list[logging.LogRecord]]:
        records: list[logging.LogRecord] = []

        class CapturingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("test.request_id_filter")
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()
        handler = CapturingHandler()
        handler.addFilter(RequestIdFilter())
        logger.addHandler(handler)
        logger.propagate = False
        return logger, records

    @pytest.mark.asyncio
    async def test_injects_request_id_inside_context(self) -> None:
        """Records emitted inside request_context carry the request_id."""
        logger, records = self._make_logger()
        async with request_context("req-123") as request_id:
            assert request_id == "req-123"
            logger.info("processing")
        assert records[0].request_id == "req-123"

    def test_no_request_id_outside_context(self) -> None:
        """Records emitted outside any context have no request_id."""
        logger, records = self._make_logger()
        logger.info("background work")
        assert not hasattr(records[0], "request_id")

    def test_explicit_request_id_not_overwritten(self) -> None:
        """An explicitly passed extra request_id wins over the contextvar."""
        logger, records = self._make_logger()
        set_request_id("ctx-id")
        try:
            logger.info("explicit", extra={"request_id": "explicit-id"})
        finally:
            clear_request_id()
        assert records[0].request_id == "explicit-id"
