"""Sensitive OTP delivery must not be logged, queued or sent to other channels."""
import json
import os
import unittest
from unittest import mock
from test_sms_gateway import GatewayCase, base_config
import sms_gateway as gw

class OtpTests(GatewayCase):
    def test_failed_delivery_does_not_persist_code_or_retry(self):
        self.sms_result = ['failed']
        profile = gw.client_profile('otp-client', {'recipients':['contact1'], 'retry':True, 'otp_only':True, 'allow_high':True})
        result = gw.notify({'key':'otp-'+'a'*32, 'text':'Code: 1234',
                            'to':['contact1'], 'kind':'otp_example', 'priority':'high'}, profile)
        self.assertEqual(result['results']['contact1']['sms'], 'failed')
        self.assertEqual(gw.load_state()['queue'], [])
        for name in os.listdir(self.tmp.name):
            if name in ('communication.jsonl', 'state.json', 'dryrun.jsonl'):
                with open(os.path.join(self.tmp.name,name), encoding='utf-8') as f:
                    self.assertNotIn('1234', f.read())

    def test_bad_payload_rejected(self):
        profile = gw.client_profile('otp-client', {'recipients':['contact1'], 'otp_only':True})
        self.assertEqual(gw.notify({'key':'x','text':'1234','to':['contact1']},profile), {'error':'bad request'})

    def test_dry_run_redacts(self):
        self.write_config(base_config(live=False))
        profile = gw.client_profile('otp-client', {'recipients':['contact1'], 'otp_only':True})
        gw.notify({'key':'otp-'+'b'*32,'text':'Code: 9876','to':['contact1']},profile)
        for name in ('dryrun.jsonl','communication.jsonl'):
            with open(os.path.join(self.tmp.name,name), encoding='utf-8') as f:
                self.assertNotIn('9876',f.read())

if __name__ == '__main__': unittest.main()
