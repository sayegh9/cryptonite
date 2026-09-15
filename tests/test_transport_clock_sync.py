"""Tests for ClockSyncMonitor and drift guardrails."""

import pytest
from cryptonite_execution.clock import (
    ClockDriftExceededError,
    ClockSample,
    ClockSyncMonitor,
)


def test_clock_sample_rtt_and_offset_calculation():
    """Verify standard NTP-style offset and RTT calculation."""
    monitor = ClockSyncMonitor()

    # Case 1: Perfectly synchronized clock with 40ms RTT
    # Send at 1000, Server sees at 1020, Receive at 1040
    # RTT = 40ms, midpoint = 1000 + 20 = 1020, offset = 1020 - 1020 = 0
    sample = monitor.compute_sample(t_send_ms=1000, server_time_ms=1020, t_recv_ms=1040)
    assert sample.rtt_ms == 40
    assert sample.offset_ms == 0.0
    assert sample.is_warning is False
    assert sample.is_blocked is False

    # Case 2: Local clock is 100ms slow (server is ahead)
    # Send at 1000, Server sees at 1120, Receive at 1040
    # RTT = 40ms, midpoint = 1020, offset = 1120 - 1020 = +100ms
    sample = monitor.compute_sample(t_send_ms=1000, server_time_ms=1120, t_recv_ms=1040)
    assert sample.rtt_ms == 40
    assert sample.offset_ms == 100.0
    assert sample.is_warning is False
    assert sample.is_blocked is False

    # Case 3: 500ms warning threshold exceeded
    sample = monitor.compute_sample(t_send_ms=1000, server_time_ms=1550, t_recv_ms=1040)
    assert sample.offset_ms == 530.0
    assert sample.is_warning is True
    assert sample.is_blocked is False
    # Assertion should not raise because block threshold is 1000ms
    monitor.assert_clock_in_bounds(sample)

    # Case 4: 1000ms blocking threshold exceeded
    sample = monitor.compute_sample(t_send_ms=1000, server_time_ms=2100, t_recv_ms=1040)
    assert sample.offset_ms == 1080.0
    assert sample.is_warning is True
    assert sample.is_blocked is True
    with pytest.raises(ClockDriftExceededError) as exc_info:
        monitor.assert_clock_in_bounds(sample)
    assert "exceeds safety threshold" in str(exc_info.value)


@pytest.mark.asyncio
async def test_clock_monitor_mock_probe():
    """Verify immediate clock probe with simulated fetcher."""
    mock_server_time = 1725700000000

    async def mock_fetcher():
        return mock_server_time

    monitor = ClockSyncMonitor(time_fetcher=mock_fetcher)
    sample = await monitor.sample_now()
    assert isinstance(sample, ClockSample)
    assert sample.server_time_ms == mock_server_time
