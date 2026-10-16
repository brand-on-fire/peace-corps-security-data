import datetime as dt
import unittest
from public_data_pipeline import apply_archive_age


class NoticeAgeTest(unittest.TestCase):
    def test_still_listed_notice_retains_its_real_date_and_reported_state(self):
        row=dict(occurredAt='2025-12-23T00:00:00Z',state='reported',
                 sourceIds=['cdc-travel-notices'],sourceMetadata=dict(provider='CDC',providerActiveNotice=True))
        apply_archive_age(row,dt.datetime(2026,10,15,tzinfo=dt.timezone.utc))
        self.assertTrue(row['historical'])
        self.assertEqual(row['state'],'reported')
        self.assertEqual(row['occurredAt'],'2025-12-23T00:00:00Z')
        row['sourceMetadata']['providerActiveNotice']=False
        apply_archive_age(row,dt.datetime(2026,10,15,tzinfo=dt.timezone.utc))
        self.assertEqual(row['state'],'expired')

    def test_unrelated_or_cancelled_records_do_not_get_revived(self):
        for state,source in [('reported','local-news'),('cancelled','cdc-travel-notices')]:
            row=dict(occurredAt='2025-12-23T00:00:00Z',state=state,sourceIds=[source],
                     sourceMetadata=dict(provider='CDC',providerActiveNotice=True))
            apply_archive_age(row,dt.datetime(2026,10,15,tzinfo=dt.timezone.utc))
            self.assertEqual(row['state'],'expired' if state=='reported' else 'cancelled')


if __name__=='__main__':
    unittest.main()
