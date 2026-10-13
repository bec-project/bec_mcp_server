"""The log cache: seeding, following, bounds, client restarts, and its place in the connection."""

from __future__ import annotations

import gc

import pytest
from log_fakes import NOW, StreamConnector, connection_for, log_message, stream_client

from bec_mcp_server.bec.connection import BecConnectionConfig
from bec_mcp_server.bec.log_cache import MAX_STORED_MESSAGE_CHARS, LogCache


def messages_of(cache: LogCache) -> list[str]:
    return [record.message for record in cache.view().records]


def ids_of(cache: LogCache) -> list[int]:
    return [record.id for record in cache.view().records]


def stream(*texts: str, start: float = NOW - 100) -> list:
    return [log_message(text, t=start + i) for i, text in enumerate(texts)]


# ---------------------------------------------------------------------------- seeding and following


def test_attach_seeds_from_the_stream_and_numbers_records_from_one():
    client = stream_client(stream("a", "b", "c"))
    cache = LogCache(10)

    cache.attach(client)

    assert cache.following
    assert messages_of(cache) == ["a", "b", "c"]
    assert ids_of(cache) == [1, 2, 3]
    # the seed asks for no more than the cache can hold
    assert client.connector.get_last_calls == [10]


def test_new_records_arrive_through_the_subscription():
    client = stream_client(stream("a"))
    cache = LogCache(10)
    cache.attach(client)

    client.connector.publish(log_message("b", t=NOW), log_message("c", t=NOW + 1))

    assert messages_of(cache) == ["a", "b", "c"]
    assert ids_of(cache) == [1, 2, 3]
    assert cache.state()["received"] == 2


def test_the_cache_drops_the_oldest_records_beyond_its_capacity():
    client = stream_client(stream("a", "b", "c", "d", "e"))
    cache = LogCache(3)
    cache.attach(client)
    assert messages_of(cache) == ["c", "d", "e"]

    client.connector.publish(log_message("f", t=NOW))

    assert messages_of(cache) == ["d", "e", "f"]
    assert ids_of(cache) == [2, 3, 4]
    assert cache.state()["evicted"] == 1


def test_records_logged_while_the_seed_is_read_are_merged_once_in_order():
    connector = StreamConnector(stream("a", "b"))
    late = log_message("c", t=NOW)
    # the record reaches both the stream the seed reads and the subscription
    connector.during_get_last = lambda: connector.publish(late)
    cache = LogCache(10)

    cache.attach(stream_client(connector=connector))

    assert messages_of(cache) == ["a", "b", "c"]
    assert cache.state()["duplicates"] == 0
    connector.publish(log_message("d", t=NOW + 1))
    assert messages_of(cache) == ["a", "b", "c", "d"]


def test_a_record_delivered_live_but_missing_from_the_seed_is_kept():
    connector = StreamConnector(stream("a"))
    late = log_message("b", t=NOW)

    def deliver_without_storing():
        # it reaches the subscription after the seed was read from the stream
        callback = connector.subscribers[0][0]()
        callback({"data": late})

    connector.during_get_last = deliver_without_storing
    cache = LogCache(10)
    cache.attach(stream_client(connector=connector))

    assert messages_of(cache) == ["a", "b"]


def test_identical_records_are_not_added_twice():
    record = log_message("same", t=NOW)
    client = stream_client([record])
    cache = LogCache(10)
    cache.attach(client)

    client.connector.publish(record)

    assert messages_of(cache) == ["same"]
    assert cache.state()["duplicates"] == 1


def test_the_same_text_at_another_time_is_a_new_record():
    client = stream_client([log_message("tick", t=NOW)])
    cache = LogCache(10)
    cache.attach(client)

    client.connector.publish(log_message("tick", t=NOW + 1))

    assert messages_of(cache) == ["tick", "tick"]


def test_overlong_messages_are_clipped_when_stored():
    client = stream_client([log_message("x" * (MAX_STORED_MESSAGE_CHARS + 50), t=NOW)])
    cache = LogCache(10)
    cache.attach(client)

    (stored,) = messages_of(cache)
    assert len(stored) < MAX_STORED_MESSAGE_CHARS + 20
    assert stored.endswith("[cut]")


def test_unreadable_stream_entries_are_counted_not_stored():
    client = stream_client([log_message("a", t=NOW)])
    cache = LogCache(10)
    cache.attach(client)

    callback = client.connector.subscribers[0][0]()
    callback({"data": None})
    callback("not a stream entry")

    assert messages_of(cache) == ["a"]
    assert cache.state()["unreadable"] == 2


def test_the_subscription_survives_garbage_collection():
    # bec_lib holds callbacks weakly; a callback nobody else references would vanish silently
    client = stream_client(stream("a"))
    cache = LogCache(10)
    cache.attach(client)
    gc.collect()

    client.connector.publish(log_message("b", t=NOW))

    assert client.connector.live_subscribers() == 1
    assert messages_of(cache) == ["a", "b"]


# ---------------------------------------------------------------------------- client lifecycle


def test_a_new_client_resumes_without_duplicates_and_picks_up_the_gap():
    first = stream_client(stream("a", "b"))
    cache = LogCache(10)
    cache.attach(first)
    cache.detach("the BEC client restarted")
    assert not cache.following
    # logged while no client was running
    gap = stream("a", "b") + [log_message("c", t=NOW)]

    second = stream_client(gap)
    cache.attach(second)

    assert messages_of(cache) == ["a", "b", "c"]
    assert ids_of(cache) == [1, 2, 3]
    assert cache.state()["subscriptions"] == 2


def test_records_from_a_replaced_client_are_ignored():
    first = stream_client(stream("a"))
    cache = LogCache(10)
    cache.attach(first)
    stale = first.connector.subscribers[0][0]

    cache.attach(stream_client(stream("a")))
    gc.collect()

    # the old follower is gone; even a callback still held elsewhere would be ignored
    assert stale() is None
    first.connector.publish(log_message("late", t=NOW))
    assert messages_of(cache) == ["a"]


def test_detach_stops_following_but_keeps_the_records():
    client = stream_client(stream("a"))
    cache = LogCache(10)
    cache.attach(client)

    cache.detach("the server shut down")
    client.connector.publish(log_message("b", t=NOW))

    state = cache.state()
    assert state["state"] == "stopped"
    assert state["problem"] == "the server shut down"
    assert messages_of(cache) == ["a"]
    assert cache.view().stopped_at is not None


def test_a_cache_that_cannot_subscribe_reports_why_and_does_not_raise():
    client = stream_client(stream("a"))
    client.connector.register_error = PermissionError("NOPERM")
    cache = LogCache(10)

    cache.attach(client)

    state = cache.state()
    assert state["state"] == "failed"
    assert "NOPERM" in state["problem"]
    assert not cache.following
    assert len(cache) == 0


def test_a_failed_seed_leaves_no_subscription_behind():
    client = stream_client(stream("a"))
    client.connector.up = False
    cache = LogCache(10)

    cache.attach(client)
    gc.collect()

    assert cache.state()["state"] == "failed"
    assert client.connector.live_subscribers() == 0


def test_state_describes_the_cache_without_io():
    cache = LogCache(5)
    assert cache.state()["state"] == "not_started"
    cache.attach(stream_client(stream("a", "b")))

    state = cache.state()
    assert (state["records"], state["capacity"], state["first_id"], state["last_id"]) == (
        2,
        5,
        1,
        2,
    )
    assert state["oldest"].endswith("+00:00")


def test_a_cache_needs_room():
    with pytest.raises(ValueError):
        LogCache(0)


# ---------------------------------------------------------------------------- in the connection


def test_the_connection_starts_the_cache_with_its_client():
    client = stream_client(stream("a", "b"))
    connection = connection_for(client)
    assert connection.state()["log_cache"]["state"] == "not_started"

    connection.connect()

    assert connection.log_cache.following
    assert connection.state()["log_cache"]["records"] == 2
    connection.shutdown()
    assert connection.state()["log_cache"]["state"] == "stopped"


def test_a_client_restart_moves_the_cache_to_the_new_client(fake_clock):
    clients = [stream_client(stream("a")), stream_client(stream("a", "b"))]
    connection = connection_for(clients[0])
    connection._client_factory = lambda config, cls: clients.pop(
        0
    )  # pylint: disable=protected-access
    connection.config = BecConnectionConfig(client_max_age_s=60)
    connection._clock = fake_clock  # pylint: disable=protected-access

    connection.connect()
    fake_clock.now += 61
    connection.connect()

    assert [r.message for r in connection.log_cache.view().records] == ["a", "b"]
    assert connection.log_cache.state()["subscriptions"] == 2
    connection.shutdown()


def test_the_cache_can_be_switched_off():
    connection = connection_for(stream_client(stream("a")), log_cache_size=0)
    connection.connect()

    assert connection.log_cache is None
    assert connection.state()["log_cache"] == {"state": "off"}
    connection.shutdown()


def test_cache_size_comes_from_the_environment_and_flags(monkeypatch):
    monkeypatch.setenv("BEC_MCP_LOG_CACHE_SIZE", "500")
    config = BecConnectionConfig.from_env()
    assert config.log_cache_size == 500
    assert config.with_overrides(log_cache_size=0).log_cache_size == 0
    with pytest.raises(SystemExit):
        config.with_overrides(log_cache_size=-1)
    monkeypatch.setenv("BEC_MCP_LOG_CACHE_SIZE", "-3")
    with pytest.raises(SystemExit):
        BecConnectionConfig.from_env()
