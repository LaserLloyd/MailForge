# Responsive UI check

`check_responsive.py` drives a **running scratch instance** of the web UI with headless Chromium at
390x844, 768x1024, 912x1000, 1180x820 and 1920x1080, on every page, and fails on: horizontal page
scroll, text squeezed into a sliver (narrow box that breaks words), buttons overlapping text or each
other, and (under 1100 px) buttons shorter than 40 px. The sticky action bar is measured in flow
position (it floats over scrolled content by design). Not part of the default unit suite.

```sh
# scratch home: never the live ~/.local/share/mailforge
export MAILFORGE_HOME=/tmp/mf-scratch MAILFORGE_NO_LISTENERS=1 MAILFORGE_UI_PORT=61777
mailforge demo --no-browser --port 61777        # or: copy a DB + config into $MAILFORGE_HOME and `mailforge serve`
python tests/ui/check_responsive.py --base http://127.0.0.1:61777 \
    --key-file $MAILFORGE_HOME/data/ui_launcher_key --message-id 1 --draft-id 1 \
    [--shots out/ --prefix after-] [--only inbox-open,settings] [--viewports 390x844,912x1000] \
    [--chromium ~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell]
```

Needs the `playwright` Python package. Exit 0 = clean, 1 = violations, 2 = could not run. It refuses
non-loopback URLs. (`mailforge demo` uses a temp home; point `--key-file` at its `data/ui_launcher_key`.)
