import unittest
from policy_evidence import classify_policy, trusted_source
URL='https://corporate.walmart.com/askwalmart/does-walmart-price-match'
class PolicyEvidenceTests(unittest.TestCase):
    def test_explicit_denial(self):
        txt='We do not offer any price matching for items previously purchased on Walmart.com that have since decreased in price.'
        r=classify_policy('walmart',txt,URL)
        self.assertEqual(r['status'],'not_eligible')
        self.assertFalse(r['eligible'])
    def test_ambiguous_is_unknown(self):
        self.assertEqual(classify_policy('walmart','Price match available at stores',URL)['status'],'unknown')
    def test_amazon_not_assumed_eligible(self):
        self.assertEqual(classify_policy('amazon','Returns within 30 days', 'https://www.amazon.com/gp/help/customer/display.html')['status'],'unknown')
    def test_external_url_rejected(self):
        self.assertFalse(trusted_source('walmart','https://walmart.com.evil.example/policy'))
        self.assertEqual(classify_policy('walmart','We do not offer any price matching for items previously purchased on Walmart.com that have since decreased in price.', 'https://evil.example')['status'],'unknown')
    def test_empty_response_unknown(self):
        self.assertEqual(classify_policy('walmart','',URL)['status'],'unknown')
if __name__=='__main__': unittest.main()
