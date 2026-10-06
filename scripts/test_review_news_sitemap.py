import unittest
from review_news_sitemap import parse_news_sitemap

def document(title='Road closed',url='https://fijisun.com.fj/news/road',date='2026-10-05T05:23:45.123Z'):
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:n="http://www.google.com/schemas/sitemap-news/0.9"><url><loc>{url}</loc><n:news><n:title>{title}</n:title><n:publication_date>{date}</n:publication_date></n:news></url></urlset>'.encode()

class NewsSitemapTests(unittest.TestCase):
    def parse(self,payload):return parse_news_sitemap(payload,{'fijisun.com.fj'})
    def test_metadata_preserved_and_no_body(self):
        self.assertEqual(self.parse(document('Lautoka &amp; Nadi')),[{'title':'Lautoka & Nadi','url':'https://fijisun.com.fj/news/road','publishedAt':'2026-10-05T05:23:45.123Z','description':''}])
    def test_wrong_namespace_or_html_rejected(self):
        for payload in [b'<html/>',b'<urlset/>',b'<rss/>']:
            with self.assertRaises(ValueError):self.parse(payload)
    def test_missing_time_and_ambiguous_entry_rejected(self):
        for payload in [document(date=''),document(date='2026-10-05T05:00:00'),document().replace(b'</n:news>',b'<n:title>Another</n:title></n:news>')]:
            with self.assertRaises(ValueError):self.parse(payload)
    def test_foreign_unsafe_or_duplicate_urls_rejected(self):
        for url in ['https://other.example/item','javascript:alert(1)','https://user:pass@fijisun.com.fj/item']:
            with self.assertRaises(ValueError):self.parse(document(url=url))
        item=document().split(b'<url>')[1].split(b'</url>')[0]
        with self.assertRaises(ValueError):self.parse(document().replace(b'</urlset>',b'<url>'+item+b'</url></urlset>'))
    def test_dtd_rejected(self):
        with self.assertRaises(ValueError):self.parse(b'<!DOCTYPE urlset>'+document())
    def test_complete_long_title_not_truncated(self):
        title='a'*10000
        self.assertEqual(self.parse(document(title))[0]['title'],title)

if __name__=='__main__':unittest.main()
