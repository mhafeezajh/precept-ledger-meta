"""The console's script (web/static/js/app.js). There is no browser in the unit suite, so these
read the source; the behaviour itself was checked in Chromium (docs/modules/web.md)."""

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[2] / "web" / "static" / "js" / "app.js"


def test_the_no_js_button_stays_hidden_after_an_htmx_swap() -> None:
    # An HX-Location answer swaps a whole page into <body> with no page load, so hiding the
    # no-JS Switch button only on DOMContentLoaded brought it back after every policy or
    # improve action. The same step must run on htmx:load.
    source = APP_JS.read_text(encoding="utf-8")
    hooks = re.findall(r'addEventListener\("(DOMContentLoaded|htmx:load)", (\w+)\)', source)
    assert sorted(hooks) == [("DOMContentLoaded", "enhance"), ("htmx:load", "enhance")]
    enhance = source[source.index("function enhance()") :]
    enhance = enhance[: enhance.index("\n  }\n")]
    assert "[data-no-js]" in enhance
    assert "[data-js-only]" in enhance
