"""Deleting a strategy: a stopped instance with no trading history can go with it; anything else blocks."""

from cryptonite_api.routes.strategies import instance_delete_blockers


def test_stopped_instance_without_history_does_not_block():
    assert instance_delete_blockers([{"instance_id": "INST-1", "status": "STOP"}], {"INST-1": 0}) == []


def test_running_instance_blocks_until_stopped():
    msgs = instance_delete_blockers([{"instance_id": "INST-1", "status": "RUNNING"}], {})
    assert msgs == ["instance INST-1 is RUNNING: stop it first"]


def test_stopped_instance_with_trading_history_blocks():
    msgs = instance_delete_blockers([{"instance_id": "INST-2", "status": "STOP"}], {"INST-2": 3})
    assert msgs == ["instance INST-2 has trading history: it cannot be removed"]


def test_no_instances_no_blockers():
    assert instance_delete_blockers([], {}) == []
