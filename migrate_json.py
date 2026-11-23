"""One-off import of your old JSON files into SQLite.
Usage:  python migrate_json.py                 -> import users only
        python migrate_json.py vip_month        -> also give previously 'verified' users 30 days of that plan (as a goodwill grant)
"""
import asyncio
import json
import sys

import db


async def main():
    await db.init()
    users = json.load(open("user_data.json"))
    for uid, u in users.items():
        await db.execute("INSERT OR IGNORE INTO users(user_id,username,full_name,joined_at) VALUES(?,?,?,?)",
                         (int(uid), u.get("username"), u.get("full_name"), db.now()))
    print(f"Imported {len(users)} users")
    if len(sys.argv) > 1:
        plan = await db.get_plan(sys.argv[1])
        assert plan, "unknown plan code"
        for uid in json.load(open("verified_users.json")):
            await db.execute("INSERT INTO subscriptions(user_id,plan_code,tier,starts_at,expires_at) VALUES(?,?,?,?,?)",
                             (int(uid), plan["code"], plan["tier"], db.now(), db.now() + plan["days"] * 86400))
            print("Granted", uid, "(they'll need /start + you can /grant again to send an invite link)")
    await db.close()


asyncio.run(main())
