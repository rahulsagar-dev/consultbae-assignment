import sqlite3

conn = sqlite3.connect("people.db")
c = conn.cursor()

tagged = c.execute("SELECT count(*) FROM people WHERE skill_category IS NOT NULL").fetchone()
print("tagged:", tagged)

audio = c.execute("SELECT count(*) FROM audio_submissions").fetchone()
print("audio submissions:", audio)

sagar = c.execute("SELECT full_name FROM people WHERE full_name LIKE '%Sagar%'").fetchall()
print("sagar rows:", sagar)

total = c.execute("SELECT count(*) FROM people").fetchone()
print("total people:", total)