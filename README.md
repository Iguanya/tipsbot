# Tips Bot

## Setup
1. `python -m venv venv && source venv/bin/activate` (Windows: `venv\Scripts\activate`)
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and fill it in (BOT_TOKEN, ADMIN_IDS, channel IDs, MPESA_TILL)
4. Make the bot an **admin** of every channel (VIP channels: enable "Invite users via link" + "Ban users")
5. (Optional) `python migrate_json.py` to import old users
6. `python main.py`

## Daily use (admins)
- Send any text/photo/video to the bot in private -> choose audience with the buttons
- /stats  /pending  /grant <id> <plan>  /revoke <id>  /setprice <plan> <kes> [stars]

## Adding a payment provider
Create `payments/yourprovider.py` with CODE, LABEL, available(), async start(update, context, plan);
register it in `payments/__init__.py`; add its CODE to PAYMENT_METHODS. Call `payments.core.complete()` when paid.

## Roles
| | User | Admin | Owner |
|---|---|---|---|
| Subscribe, pay, /status, /help, /myid | yes | yes | yes |
| Publish tips (send content to the bot) | | yes | yes |
| /admin panel, /stats, /pending, approve/reject payments, /grant | | yes | yes |
| /revoke, /setprice, /register_c2b | | | yes |
| /addadmin, /removeadmin, /admins | | | yes |

Owners = `ADMIN_IDS` in `.env`. Admins are added from inside the bot with `/addadmin <user_id>`.
