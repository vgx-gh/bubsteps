"""
Manual admin tool: reset a user's BubSteps password by hand.

This is a standalone script, separate from the running app - it does not
touch app.py or need the server to be running. It talks directly to
auth.db (the shared login database), which is exactly where the app itself
looks up passwords, so a password set this way works immediately.

Usage (run from the project folder, same place as app.py):

    python reset_password.py someone@example.com newpassword123

- The new password must be at least 8 characters (same rule the signup
  page enforces), so real accounts and manually-reset ones stay consistent.
- If no account exists with that email, the script says so and changes
  nothing.
- This never touches anyone's actual tracker data (entries/journal/weight/
  settings) - it only updates the one password_hash column for that one
  user's row in auth.db.
"""

import sys
import sqlite3
from werkzeug.security import generate_password_hash

AUTH_DB_PATH = "auth.db"


def main():
    if len(sys.argv) != 3:
        print("Usage: python reset_password.py <email> <new_password>")
        sys.exit(1)

    email = sys.argv[1].strip().lower()
    new_password = sys.argv[2]

    if len(new_password) < 8:
        print("Password needs to be at least 8 characters - nothing was changed.")
        sys.exit(1)

    conn = sqlite3.connect(AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row

    user = conn.execute("SELECT id, name FROM users WHERE email = ?", (email,)).fetchone()
    if user is None:
        print(f"No account found for {email} - nothing was changed.")
        conn.close()
        sys.exit(1)

    conn.execute(
        "UPDATE users SET password_hash = ? WHERE email = ?",
        (generate_password_hash(new_password), email),
    )
    conn.commit()
    conn.close()

    print(f"Password updated for {user['name']} ({email}). They can log in with the new password now.")


if __name__ == "__main__":
    main()
