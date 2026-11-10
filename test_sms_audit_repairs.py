"""Regression tests against synthetic gateway state, without modem access."""
import json
import os
import unittest
from unittest import mock

import sms_gateway as gateway
import sms_inbound as inbound
from test_sms_gateway import GatewayCase, base_config


class AuditRepairs(GatewayCase):
    def test_corrupt_state_is_not_replaced(self):
        path = os.path.join(self.tmp.name, 'state.json')
        with open(path, 'w', encoding='utf-8') as file: file.write('{broken')
        with self.assertRaises(ValueError): gateway.notify({'key': 'test', 'text': 'test'})
        with open(path, encoding='utf-8') as file: self.assertEqual(file.read(), '{broken')
        self.assertFalse(self.sms_calls)

    def test_otp_is_not_limited_by_message_quota(self):
        self.write_config(base_config(daily_limit=1, client_daily_limit=1))
        profile = gateway.client_profile('otp-client', {'recipients': ['contact1'], 'otp_only': True, 'allow_high': True})
        for key in ('a', 'b'):
            result = gateway.notify({'key': 'otp-' + key * 32, 'text': 'Code: 1234',
                                     'to': ['contact1'], 'priority': 'high'}, profile)
            self.assertEqual(result['results']['contact1']['sms'], 'sent')

    def test_inbound_state_corruption_fails_closed(self):
        path = os.path.join(self.tmp.name, 'inbound_state.json')
        with open(path, 'w', encoding='utf-8') as file: file.write('{broken')
        service = inbound.Inbound(lambda: {}, self.tmp.name, lambda *args: None)
        with self.assertRaises(ValueError): service.load()
        with open(path, encoding='utf-8') as file: self.assertEqual(file.read(), '{broken')

    def test_country_code_is_part_of_identity(self):
        self.assertTrue(inbound.same_number('+48 600 000 001', '600000001'))
        self.assertFalse(inbound.same_number('+48 600 000 001', '+49 600 000 001'))


if __name__ == '__main__': unittest.main()
