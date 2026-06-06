"""xbot — X/Twitter quote-tweet curator bot.

Finds viral consumer-app content skills from your warmed feed and quote-tweets
them with compact, growth-first commentary. See ARCHITECTURE.md for the design.
"""

__version__ = "0.1.0"

# The UI prints unicode (bullets, box-drawing). Windows consoles default to
# cp1252 and would crash — force UTF-8 on import so every entry path is safe.
import sys as _sys

for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass
