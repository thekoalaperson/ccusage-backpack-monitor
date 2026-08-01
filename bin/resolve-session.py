#!/usr/bin/env python3
"""Print the session the monitor should follow, as: <sid><TAB><transcript>

Used by bin/open-now.sh, which cannot identify the session itself: slash
commands run without CLAUDE_SESSION_ID, so its only fallback was "newest
transcript in the directory derived from $PWD". That picks a *subagent*
transcript whenever an agent has run with a different cwd, and reports one
agent's spend as the whole session's.

Prints nothing and exits 1 when no session can be determined.

Usage: resolve-session.py [pwd] [session-id]
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from pricing import Pricing        # noqa: E402
from usage import Scanner, Resolver  # noqa: E402

pwd = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
sid = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("CLAUDE_SESSION_ID", "")

try:
    scanner = Scanner(Pricing(allow_refresh=False))
    resolved, path = Resolver(scanner).resolve(pwd=pwd, sid=sid or None)
    try:
        scanner.save()
    except Exception:
        pass
except Exception:
    sys.exit(1)

if not resolved or not path:
    sys.exit(1)
sys.stdout.write("%s\t%s\n" % (resolved, path))
