"""Minimal admin CLI to provision users and API keys before the billing flow
exists. Prints the plaintext key once (never stored).

    python -m app.admin create-user alice@example.com --plan pro
    python -m app.admin create-key <user_id> --label "laptop"
    python -m app.admin list-keys <user_id>
"""
from __future__ import annotations

import argparse

from app import db
from app.security import PLAN_LIMITS, generate_key


def create_user(email: str, plan: str) -> None:
    if plan not in PLAN_LIMITS:
        raise SystemExit(f"plan must be one of {list(PLAN_LIMITS)}")
    row = db.query_one(
        "INSERT INTO users (email, plan, subscription_status) "
        "VALUES (%s, %s, 'active') "
        "ON CONFLICT (email) DO UPDATE SET plan=EXCLUDED.plan, "
        "subscription_status='active' RETURNING id",
        (email, plan),
    )
    print(f"user_id={row['id']} email={email} plan={plan}")


def create_key(user_id: str, label: str | None) -> None:
    plaintext, key_hash = generate_key()
    db.execute(
        "INSERT INTO api_keys (key_hash, user_id, label) VALUES (%s, %s, %s)",
        (key_hash, user_id, label),
    )
    print("API key (shown once, store it now):")
    print(f"  {plaintext}")


def list_keys(user_id: str) -> None:
    rows = db.query(
        "SELECT label, created_at, revoked_at FROM api_keys WHERE user_id=%s ORDER BY created_at",
        (user_id,),
    )
    for r in rows:
        state = "revoked" if r["revoked_at"] else "active"
        print(f"  [{state}] {r['label']} created={r['created_at']}")


def main() -> None:
    p = argparse.ArgumentParser(prog="app.admin")
    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("create-user")
    u.add_argument("email")
    u.add_argument("--plan", default="developer")

    k = sub.add_parser("create-key")
    k.add_argument("user_id")
    k.add_argument("--label", default=None)

    l = sub.add_parser("list-keys")
    l.add_argument("user_id")

    args = p.parse_args()
    if args.cmd == "create-user":
        create_user(args.email, args.plan)
    elif args.cmd == "create-key":
        create_key(args.user_id, args.label)
    elif args.cmd == "list-keys":
        list_keys(args.user_id)


if __name__ == "__main__":
    main()
