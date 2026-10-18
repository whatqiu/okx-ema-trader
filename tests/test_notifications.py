"""Outbox deduplication, retries and SMTP isolation (no real mail sent)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "platform" / "backend"))
import notifications as mail


class EmailTests(unittest.TestCase):
    def test_dedupe_retry_restart(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(mail, "DATABASE", Path(tmp) / "outbox.db"), \
             patch.object(mail, "settings", return_value={"enabled": True}):
            mail.enqueue("BTC-USDT-SWAP", "long", 123000, "cross")
            mail.enqueue("BTC-USDT-SWAP", "long", 123000, "cross")
            with patch.object(mail, "send", side_effect=OSError("offline")):
                with self.assertRaises(OSError):
                    mail.deliver_pending()
            with patch.object(mail, "send") as sender:
                mail.deliver_pending()
                mail.deliver_pending()
                self.assertEqual(sender.call_count, 1)
            mail.enqueue("BTC-USDT-SWAP", "short", 456000, "cross")
            with patch.object(mail, "send") as sender:
                mail.deliver_pending()
                self.assertEqual(sender.call_count, 1)

    def test_disabled_does_not_queue(self):
        with patch.object(mail, "settings", return_value=None), patch.object(mail, "connect") as conn:
            mail.enqueue("BTC-USDT-SWAP", "long", 123000, "cross")
            mail.deliver_pending()
            conn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
