"""Reset a Zerko account password from the machine itself.

Run this when you cannot sign in. It only works with filesystem access to the
server, which is the point: being at the machine IS the proof of ownership.

The password is typed here, hashed immediately, and written as a hash. It is
never echoed to the screen, never logged, and never stored in readable form.

    python reset_password.py
"""

import getpass
import sys

from database import SessionLocal, User
from auth import get_password_hash, revoke_sessions

MIN_LEN = 10
TOO_COMMON = {"admin123", "password", "123456789", "changeme", "zerko1234",
              "password123", "qwerty123"}


def main():
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.id).all()
        if not users:
            print("  No accounts exist yet - open Zerko in your browser and run the setup wizard.")
            return 1

        print("\n  Accounts on this server:\n")
        for i, u in enumerate(users, 1):
            print(f"    {i}. {u.username}   ({u.role})")

        choice = input(f"\n  Which one? [1-{len(users)}]: ").strip()
        try:
            user = users[int(choice) - 1]
        except (ValueError, IndexError):
            print("  Not a valid choice.")
            return 1

        print(f"\n  Setting a new password for '{user.username}'.")
        print("  Nothing appears as you type - that is normal.\n")

        pw = getpass.getpass("  New password: ")
        if len(pw) < MIN_LEN:
            print(f"  Too short - use at least {MIN_LEN} characters.")
            return 1
        if pw.lower() in TOO_COMMON:
            print("  That password is far too common. Pick another.")
            return 1

        again = getpass.getpass("  Type it again: ")
        if pw != again:
            print("  They do not match. Nothing was changed.")
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
