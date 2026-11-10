"""Test configurable OTP patterns without network or real SMS."""
import unittest
import sms_gateway as gw
from test_sms_gateway import GatewayCase


class OtpPolicyTests(GatewayCase):
    def test_custom_policy_and_nonmatching_message(self):
        profile = gw.client_profile('test', {'recipients':['contact1'], 'otp_only':True,
            'otp_text_pattern':r'PIN [0-9]{4}', 'otp_key_pattern':r'challenge-[a-f0-9]{32}'})
        bad = {'key':'challenge-'+'a'*32, 'text':'PIN invalid', 'to':['contact1']}
        self.assertEqual(gw.notify(bad, profile), {'error':'bad request'})
        good = {**bad, 'text':'PIN 1234'}
        self.assertEqual(gw.notify(good, profile)['results']['contact1']['sms'], 'sent')

    def test_invalid_pattern_fails_closed(self):
        profile = gw.client_profile('test', {'recipients':['contact1'], 'otp_only':True,
            'otp_text_pattern':'[', 'otp_key_pattern':'.+'})
        self.assertEqual(gw.notify({'key':'x','text':'anything','to':['contact1']}, profile),
                         {'error':'bad request'})


if __name__ == '__main__': unittest.main()
