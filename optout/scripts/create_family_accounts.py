"""Create a login for every family member that is in the database (username = person id, e.g. sam, jordan, riley).
Each gets a strong random password and must change it at first sign-in. Passwords are printed ONCE and never stored in plain text.
Admin accounts (alex) and accounts that have already been used are not touched.

On the server (Portainer > Containers > fletcher-fortis-app > Console, /bin/sh):
    python /app/optout/scripts/create_family_accounts.py
Or, easier: Family & accounts > "Generate logins for everyone" in the app (admin only)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db, auth
c = db.init()
people = c.execute("SELECT * FROM people ORDER BY rowid").fetchall()
if not people: sys.exit("No family members in the database yet. Add them in the app first (Add family member).")
rows = auth.generate_family_logins(c, people)
if not rows: print("Every family member already has an account that is in use (or is the admin). Nothing changed.")
for pid, name, user, pw in rows: print(f"{name:<28} username: {user:<14} password: {pw}")
if rows: print("\nGive each person their password privately. They choose their own password at first sign-in.")
