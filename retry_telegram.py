"""Retry pending or failed teacher notifications after configuring Telegram."""

from app import TELEGRAM_CHAT_ID, TELEGRAM_TOKEN, connect, deliver_notification


def main() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Set DELIVERY_LAB_TELEGRAM_TOKEN and DELIVERY_LAB_TELEGRAM_CHAT_ID first.")
    with connect() as connection:
        ids = [
            row["notification_id"]
            for row in connection.execute(
                "SELECT notification_id FROM notifications WHERE delivery_status IN ('pending', 'failed') ORDER BY notification_id"
            )
        ]
    results = [deliver_notification(notification_id) for notification_id in ids]
    print(f"Checked {len(ids)} notifications: {results.count('sent')} sent, {results.count('failed')} failed.")


if __name__ == "__main__":
    main()
