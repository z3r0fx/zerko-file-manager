"""Reset a Zerko account password from the machine itself.

Run this when you cannot sign in. It only works with filesystem access to the
server, which is the point: being at the machine IS the proof of ownership.

The password is hashed immediately and written as a hash. It is never logged
or stored in readable form.

    python reset_password.py

Typing is hidden by default. Some Windows terminals do not pass hidden input
through cleanly (the two entries then "do not match" even when typed
identically), so if the entries differ this asks again with what you type
shown on screen. To start in that mode: python reset_password.py --show
"""

import getpass
import sys

from database import SessionLocal, User
from auth import get_password_hash, revoke_sessions

MIN_LEN = 10
TOO_COMMON = {"admin123", "password", "123456789", "changeme", "zerko1234",
              "password123", "qwerty123"}


def _clean(text: str) -> str:
    # Windows consoles can hand over a trailing carriage return; it is never
    # part of a password.
    return (text or "").rstrip("\r\n")


def _ask_hidden(prompt: str) -> str:
    return _clean(getpass.getpass(prompt))


def _ask_visible(prompt: str) -> str:
    return _clean(input(prompt))


def _problem_with(pw: str):
    if len(pw) < MIN_LEN:
        return f"Too short - use at least {MIN_LEN} characters."
    if pw.lower() in TOO_COMMON:
        return "That password is far too common. Pick another."
    return None


def _choose_password(show: bool):
    """Returns the new password, or None if the person gave up."""
    if not show:
        print("  Nothing appears as you type - that is normal.\n")
        for _ in range(2):
            pw = _ask_hidden("  New password: ")
            bad = _problem_with(pw)
            if bad:
                print(f"  {bad}")
                return None
            again = _ask_hidden("  Type it again: ")
            if pw == again:
                return pw
            print(f"\n  They do not match (first entry {len(pw)} characters, "
                  f"second {len(again)}).")
            print("  Your terminal may not be passing hidden typing through "
                  "properly.")
            print("  Trying again with your typing shown on screen.\n")
            break

    print("  Your typing will be SHOWN on screen this time - make sure nobody "
          "is looking.\n")
    for attempt in range(3):
        pw = _ask_visible("  New password: ")
        bad = _problem_with(pw)
        if bad:
            print(f"  {bad}\n")
            continue
        print(f"  ({len(pw)} characters)")
        ok = input("  Use this password? [Y/n]: ").strip().lower()
        if ok in ("", "y", "yes"):
            return pw
        print()
    return None


def main():
    show = "--show" in sys.argv[1:]
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.id).all()
        if not users:
            print("  No accounts exist yet - open Zerko in your browser and run the setup wizard.")
            return 1

        print("\n  Accounts on this server:\n")
        for i, u in enumerate(users, 1):
            print(f"    {i}. {u.username}   ({u.role})")

        choice = _clean(input(f"\n  Which one? [1-{len(users)}]: ")).strip()
        try:
            user = users[int(choice) - 1]
        except (ValueError, IndexError):
            print("  Not a valid choice.")
            return 1

        print(f"\n  Setting a new password for '{user.username}'.")
        pw = _choose_password(show)
        if pw is None:
            print("\n  Nothing was changed.\n")
            return 1

        user.hashed_password = get_password_hash(pw)
        # Drop any existing sessions: if someone else was signed in as this
        # account, a password reset should end that too.
        revoke_sessions(user)
        user.is_active = True
        db.commit()

        print(f"\n  Done. Sign in as '{user.username}' with the new password.")
        print("  Any existing sessions for that account have been signed out.")
        print("  If sign-in was being refused as 'too many attempts', restart Zerko")
        print("  (close its window and start it again) to clear that too.\n")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
