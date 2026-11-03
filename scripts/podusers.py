#!/usr/bin/env python3
"""podusers — the admin's view of the people who use the skill (users.py; PODNOTE_STATE/users.json).

    podusers.py list                               everyone: name, role, platforms, shows followed, limit
    podusers.py rename <person> = <name>           the name used in messages and notices
    podusers.py block <person>  / allow <person>   stop (or restore) someone: nothing is queued or sent for them
    podusers.py limit <person> = <n>               episodes they may queue a day (default PODNOTE_USER_DAILY_LIMIT, 10)
    podusers.py identity <person> = <platform:id>  another identity of theirs ("telegram:<id>", "whatsapp:<number>")
    podusers.py admin <person>                     make them an admin (at a shell, for the first one; else by an admin)

<person> is a key or a name. Admin only: anyone else is refused. Phone numbers and ids are never
printed, only which platforms a person uses.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import settings  # noqa: E402,F401  (importing it loads the project .env)
import podshows  # noqa: E402
import users  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["list", "rename", "block", "allow", "limit", "identity", "admin"])
    parser.add_argument("rest", nargs="*")
    args = parser.parse_args()
    if not users.is_admin(users.current()[1]):
        print(f"Only {users.admin()[1].get('name', 'the admin')} can see or change who uses this.")
        return 1
    who, value = podshows.assignment(" ".join(args.rest))
    if args.command == "list":
        registry = podshows.load()
        for key, user in sorted(users.load().items()):
            platforms = sorted({identity.split(":", 1)[0] for identity in user.get("identities", [])})
            follows = sum(1 for show in registry.values() if key in podshows.followers_of(show))
            limit = "" if users.is_admin(user) else f", {users.limit(user)} a day"
            print(f"{user.get('name', key)} ({user.get('role', 'member')}{limit}): {', '.join(platforms) or 'no platform'}; "
                  f"follows {follows} show{'s' * (follows != 1)}")
        return 0
    with users.editing() as everyone:
        key = users.pick(everyone, who)
        person = everyone[key]
        if args.command == "rename":
            person["name"] = value.strip() or person["name"]
            message = f"{key} is now called {person['name']}."
        elif args.command in ("block", "allow"):
            if users.is_admin(person):
                raise SystemExit("the admin cannot be blocked")
            person["role"] = "blocked" if args.command == "block" else "member"
            message = f"{person['name']}: {'blocked' if args.command == 'block' else 'a member again'}."
        elif args.command == "admin":
            person["role"] = "admin"
            message = f"{person['name']}: an admin."
        elif args.command == "limit":
            person["limit_per_day"] = max(0, int(value))
            message = f"{person['name']}: up to {person['limit_per_day']} episodes a day."
        else:
            identity = users.normalize(value)
            if not identity:
                raise SystemExit("give it as platform:id, for example whatsapp:<number> or telegram:<id>")
            other = users.find(identity, everyone)
            if other and other != key:
                raise SystemExit(f"that identity is already {everyone[other]['name']}'s")
            person["identities"] = list(dict.fromkeys(person.get("identities", []) + [identity]))
            message = f"{person['name']}: now also on {identity.split(':', 1)[0]}."
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
