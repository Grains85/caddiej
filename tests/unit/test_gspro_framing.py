"""Framing a stream that has no message boundaries.

Every case here is something a real device or network does. Open Connect gives
no length prefix and no delimiter, so this is where most of the risk lives.
"""

from caddiej.adapters.gspro.framing import JsonFramer


def test_one_message_in_one_chunk() -> None:
    framer = JsonFramer()

    assert framer.feed(b'{"a": 1}') == [b'{"a": 1}']


def test_several_messages_in_one_chunk() -> None:
    # Devices write messages back to back with nothing in between.
    framer = JsonFramer()

    assert framer.feed(b'{"a": 1}{"b": 2}{"c": 3}') == [
        b'{"a": 1}',
        b'{"b": 2}',
        b'{"c": 3}',
    ]


def test_a_message_split_across_chunks() -> None:
    framer = JsonFramer()

    assert framer.feed(b'{"a":') == []
    assert framer.feed(b" 1") == []
    assert framer.feed(b"}") == [b'{"a": 1}']


def test_a_message_split_one_byte_at_a_time() -> None:
    framer = JsonFramer()
    payload = b'{"DeviceID": "LM", "ShotNumber": 12}'

    collected = [message for byte in payload for message in framer.feed(bytes([byte]))]

    assert collected == [payload]


def test_nested_objects_are_one_message() -> None:
    framer = JsonFramer()
    payload = b'{"BallData": {"Speed": 1.0}, "Options": {"Nested": {"Deep": true}}}'

    assert framer.feed(payload) == [payload]


def test_braces_inside_strings_do_not_split_a_message() -> None:
    # The bug this prevents: a message whose text contains a brace would
    # otherwise desynchronize the framer for the rest of the connection.
    framer = JsonFramer()
    payload = b'{"Message": "shot {received} ok", "Code": 200}'

    assert framer.feed(payload) == [payload]


def test_escaped_quotes_inside_strings_are_handled() -> None:
    framer = JsonFramer()
    payload = rb'{"Message": "he said \"nice {shot}\" loudly"}'

    assert framer.feed(payload) == [payload]


def test_a_trailing_backslash_before_a_quote_does_not_confuse_the_framer() -> None:
    framer = JsonFramer()
    payload = rb'{"Path": "C:\\sim\\", "Code": 200}'

    assert framer.feed(payload) == [payload]


def test_whitespace_and_newlines_between_messages_are_skipped() -> None:
    framer = JsonFramer()

    assert framer.feed(b'\r\n {"a": 1} \n\n {"b": 2}\n') == [b'{"a": 1}', b'{"b": 2}']
    assert framer.skipped_bytes > 0


def test_null_padding_between_messages_is_skipped() -> None:
    # Some devices pad writes with NULs.
    framer = JsonFramer()

    assert framer.feed(b'\x00\x00{"a": 1}\x00') == [b'{"a": 1}']


def test_garbage_before_a_message_is_skipped_rather_than_breaking_the_link() -> None:
    framer = JsonFramer()

    assert framer.feed(b'not json at all {"a": 1}') == [b'{"a": 1}']


def test_an_oversized_message_is_dropped_and_the_stream_resynchronises() -> None:
    # A device stuck mid-message must cost one message, not the connection.
    framer = JsonFramer(max_message_bytes=32)

    assert framer.feed(b'{"padding": "' + b"x" * 100) == []
    assert framer.oversized_messages == 1

    assert framer.feed(b'{"a": 1}') == [b'{"a": 1}']


def test_an_unterminated_message_is_held_not_emitted() -> None:
    framer = JsonFramer()

    assert framer.feed(b'{"a": 1') == []
    assert framer.pending_bytes == 7


def test_invalid_utf8_is_carried_through_as_bytes() -> None:
    # Framing works on bytes; decoding is validation's problem, so a mangled
    # message becomes a rejected message rather than a dead connection.
    framer = JsonFramer()
    payload = b'{"a": "\xff\xfe"}'

    assert framer.feed(payload) == [payload]


def test_a_closing_brace_with_no_opening_one_is_ignored() -> None:
    framer = JsonFramer()

    assert framer.feed(b'}}}{"a": 1}') == [b'{"a": 1}']


def test_the_framer_keeps_working_after_hundreds_of_messages() -> None:
    framer = JsonFramer()
    payload = b'{"ShotNumber": 1}'

    for _ in range(500):
        assert framer.feed(payload) == [payload]

    assert framer.pending_bytes == 0
