"""SIG client: confirmed order payload, YES-terms mapping, auth and idempotency."""
import base64
import json
import os
import unittest
from unittest import mock

import sig_client
from arb_engine import Book
from sig_client import Client, decode_supabase_cookie, idempotency_key, levels_from_response, orders_for

SESSION = {"access_token": "AT", "refresh_token": "RT", "user": {"id": "86ccda88"}}
ENC = "base64-" + base64.urlsafe_b64encode(json.dumps(SESSION).encode()).decode().rstrip("=")
COOKIE = f"foo=1; sb-abc-auth-token.0={ENC[:20]}; sb-abc-auth-token.1={ENC[20:]}"


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def client(cookie=COOKIE):
    """A client built only from `cookie`, never from the developer's real .env."""
    env = {k: v for k, v in os.environ.items() if k not in ("SIG_COOKIE", "SIG_ACCESS_TOKEN", "SIG_PROFILE_ID")}
    with mock.patch.object(sig_client, "load_env"), mock.patch.dict(os.environ, env, clear=True):
        return Client(cookie=cookie)


class ClientTests(unittest.TestCase):
    def test_live_books_format_becomes_levels(self):
        j = {"books": [{"exchangeId": 1075, "bids": [{"price": 0.015, "quantity": 883}, {"price": 0.01, "quantity": 1050}],
                        "asks": [{"price": 0.035, "quantity": 1000}]}]}
        b = Book.from_levels(386, levels_from_response(j))
        self.assertEqual(b.bids[0], (0.015, 883))
        self.assertEqual(b.asks[0], (0.035, 1000))
        self.assertEqual(b.exchange_id, 1075)
        self.assertEqual(levels_from_response({"levels": [1]}), [1])

    def test_yes_intent_maps_to_signed_engine_orders(self):
        self.assertEqual(orders_for("BUY", 0.05, 500), [{"orderType": "BUY", "quantity": 500, "priceLimit": 0.05}])
        # Short YES is bought as NO: negative quantity, NO-terms price.
        self.assertEqual(orders_for("SELL", 0.93, 500), [{"orderType": "BUY", "quantity": -500, "priceLimit": 0.07}])
        self.assertEqual(orders_for("SELL", 0.2, 300, holdings=100),
                         [{"orderType": "SELL", "quantity": 100, "priceLimit": 0.2},
                          {"orderType": "BUY", "quantity": -200, "priceLimit": 0.8}])
        self.assertEqual(orders_for("BUY", 0.3, 50, holdings=-80), [{"orderType": "SELL", "quantity": -50, "priceLimit": 0.7}])

    def test_chunked_supabase_cookie_decodes(self):
        d = decode_supabase_cookie(COOKIE)
        self.assertEqual(d["access_token"], "AT")
        self.assertEqual(d["user"]["id"], "86ccda88")
        self.assertEqual(decode_supabase_cookie("foo=1"), {})

    def test_place_raw_body_matches_site(self):
        body = client().place_raw(1042, "BUY", 0.07, -500)["body"]
        self.assertEqual(set(body), {"createdAt", "exchangeId", "profileId", "orderType", "priceLimit",
                                     "quantity", "open", "tournamentId", "idempotencyKey"})
        self.assertEqual(body["profileId"], "86ccda88")
        self.assertEqual(body["quantity"], -500)
        self.assertIs(body["open"], True)
        self.assertTrue(body["createdAt"].endswith("Z"))

    def test_idempotency_keys_are_stable_per_client_order(self):
        c = client()
        a = c.place(1, 1042, "SELL", 0.2, 300, holdings=100, client_order_id="run1:0")
        b = c.place(1, 1042, "SELL", 0.2, 300, holdings=100, client_order_id="run1:0")
        keys = [o["body"]["idempotencyKey"] for o in a["orders"]]
        self.assertEqual(keys, [o["body"]["idempotencyKey"] for o in b["orders"]])
        self.assertEqual(len(set(keys)), 2)
        self.assertEqual(keys[0], idempotency_key("run1:0", 0))
        self.assertNotEqual(keys[0], idempotency_key("run1:1", 0))

    def test_unconfirmed_payload_never_posts(self):
        c = client()
        with mock.patch.object(sig_client, "PLACE_PAYLOAD_CONFIRMED", False), \
                mock.patch.object(c.s, "post") as post:
            resp = c.place(1, 1042, "BUY", 0.05, 10, dry_run=False)
        post.assert_not_called()
        self.assertTrue(resp["dryRun"])

    def test_live_place_uses_bearer_and_sums_fills(self):
        c = client()
        with mock.patch.object(sig_client, "PLACE_PAYLOAD_CONFIRMED", True), \
                mock.patch.object(c.s, "post", side_effect=[Resp(200, {"quantityTraded": 100}),
                                                            Resp(200, {"quantityTraded": -150})]) as post:
            resp = c.place(1, 1042, "SELL", 0.2, 300, holdings=100, dry_run=False, client_order_id="x")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer AT"})
        self.assertEqual(resp["filledQuantity"], 250)
        self.assertEqual(resp["clientOrderId"], "x")

    def test_unknown_outcome_stops_remaining_sub_orders(self):
        c = client()
        with mock.patch.object(sig_client, "PLACE_PAYLOAD_CONFIRMED", True), \
                mock.patch.object(c.s, "post", return_value=Resp(502, {"error": "gateway"})) as post:
            resp = c.place(1, 1042, "SELL", 0.2, 300, holdings=100, dry_run=False)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(resp["_unknown"])

    def test_missing_fill_field_is_unknown(self):
        c = client()
        with mock.patch.object(sig_client, "PLACE_PAYLOAD_CONFIRMED", True), \
                mock.patch.object(c.s, "post", return_value=Resp(200, {"orderId": "o1", "filled": 10})) as post:
            resp = c.place(1, 1042, "SELL", 0.2, 300, holdings=100, dry_run=False)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(resp["_unknown"])

    def test_rejections_raise(self):
        c = client()
        with mock.patch.object(sig_client, "PLACE_PAYLOAD_CONFIRMED", True):
            with mock.patch.object(c.s, "post", return_value=Resp(401, {})):
                with self.assertRaises(PermissionError):
                    c.place_raw(1042, "BUY", 0.05, 10, dry_run=False)
            with mock.patch.object(c.s, "post", return_value=Resp(400, {"error": "tick"})):
                with self.assertRaises(RuntimeError):
                    c.place_raw(1042, "BUY", 0.05, 10, dry_run=False)

    def test_live_place_without_session_refuses(self):
        c = client("foo=1")
        with mock.patch.object(sig_client, "PLACE_PAYLOAD_CONFIRMED", True), \
                mock.patch.object(c.s, "post") as post:
            with self.assertRaises(PermissionError):
                c.place_raw(1042, "BUY", 0.05, 10, dry_run=False)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
