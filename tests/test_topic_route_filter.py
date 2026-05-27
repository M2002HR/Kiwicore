from __future__ import annotations

from kiwi.service import KiwiService
from kiwi.types import ChannelRoute, IncomingChannelMessage


def _incoming(topic_id: int | None) -> IncomingChannelMessage:
    return IncomingChannelMessage(
        update_id=1,
        source_channel_id="-1001",
        source_channel_username="@src",
        message_id=10,
        date=None,
        text="x",
        caption=None,
        medias=[],
        raw={},
        source_topic_id=topic_id,
    )


def test_route_accepts_incoming_topic_when_route_has_no_topic() -> None:
    route = ChannelRoute(
        name="r",
        source_channel_username="@src",
        destination_channel_username="@dst",
        channel_script="default_channel_script.py",
    )
    assert KiwiService._route_accepts_incoming_topic(route, _incoming(None)) is True
    assert KiwiService._route_accepts_incoming_topic(route, _incoming(1556041)) is True


def test_route_accepts_only_matching_topic_when_topic_is_set() -> None:
    route = ChannelRoute(
        name="r",
        source_channel_username="@src",
        source_topic_id=1556041,
        destination_channel_username="@dst",
        channel_script="default_channel_script.py",
    )
    assert KiwiService._route_accepts_incoming_topic(route, _incoming(1556041)) is True
    assert KiwiService._route_accepts_incoming_topic(route, _incoming(1556042)) is False
    assert KiwiService._route_accepts_incoming_topic(route, _incoming(None)) is False
