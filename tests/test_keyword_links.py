from __future__ import annotations

import json
from pathlib import Path

from kiwi.keyword_links import KeywordLinker
from kiwi.types import OutputMessageKind, ScriptOutputMessage


def test_keyword_linker_preserves_reply_markup_and_footer_flag(tmp_path: Path) -> None:
    (tmp_path / "channels.json").write_text("[]", encoding="utf-8")
    (tmp_path / "keyword_links.json").write_text(
        json.dumps(
            [
                {
                    "destination": "@pirashki_ai",
                    "link": "https://ble.ir/pirashki_ai",
                    "keywords": ["Prompt"],
                }
            ]
        ),
        encoding="utf-8",
    )

    linker = KeywordLinker(channels_config_path=str(tmp_path / "channels.json"))
    markup = {"inline_keyboard": [[{"text": "ورود", "url": "https://ble.ir/pirashki_bot?start=pp_x"}]]}
    src = ScriptOutputMessage(
        type=OutputMessageKind.PHOTO,
        path="photo_1",
        caption="Prompt text",
        reply_markup=markup,
        append_destination_footer=False,
    )

    out = linker.apply("@pirashki_ai", [src])
    assert len(out) == 1
    assert out[0].caption == "[Prompt](https://ble.ir/pirashki_ai) text"
    assert out[0].reply_markup == markup
    assert out[0].append_destination_footer is False
