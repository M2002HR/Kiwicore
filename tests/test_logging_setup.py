from __future__ import annotations

from kiwi import logging_setup


def test_recent_logs_filters_by_level_logger_and_message() -> None:
    with logging_setup._LOG_LOCK:  # noqa: SLF001
        backup = list(logging_setup._LOG_BUFFER)  # noqa: SLF001
        logging_setup._LOG_BUFFER.clear()  # noqa: SLF001
        logging_setup._LOG_BUFFER.extend(  # noqa: SLF001
            [
                {"ts": "t1", "level": "INFO", "logger": "kiwi.service", "message": "route completed"},
                {"ts": "t2", "level": "ERROR", "logger": "kiwi.script_runner", "message": "channel script timeout"},
                {"ts": "t3", "level": "WARNING", "logger": "kiwi.service", "message": "network retry"},
            ]
        )

    try:
        out_level = logging_setup.recent_logs(level="error")
        assert len(out_level) == 1
        assert out_level[0]["level"] == "ERROR"

        out_logger = logging_setup.recent_logs(logger_name="script")
        assert len(out_logger) == 1
        assert out_logger[0]["logger"] == "kiwi.script_runner"

        out_message = logging_setup.recent_logs(message_contains="timeout")
        assert len(out_message) == 1
        assert "timeout" in str(out_message[0]["message"]).lower()

        out_combo = logging_setup.recent_logs(level="error", logger_name="script", message_contains="timeout")
        assert len(out_combo) == 1
        assert out_combo[0]["ts"] == "t2"
    finally:
        with logging_setup._LOG_LOCK:  # noqa: SLF001
            logging_setup._LOG_BUFFER.clear()  # noqa: SLF001
            logging_setup._LOG_BUFFER.extend(backup)  # noqa: SLF001
